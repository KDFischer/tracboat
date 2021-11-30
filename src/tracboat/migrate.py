# -*- coding: utf-8 -*-

import logging
import os
import re
import difflib
from itertools import chain
from sys import exit
from pprint import pprint

import six

from tracboat import trac2down
from tracboat.gitlab import direct  # TODO selectable mode (api/direct)
from tracboat.gitlab import model
from tracboat.labels import *
from tracboat.users import *
from . import api as gitlab_api

__all__ = ['migrate']

LOG = logging.getLogger(__name__)

STATUS_AS_LABEL = False
TICKET_PRIORITY_TO_ISSUE_LABEL = {
    'high': 'prio:high',
    # 'medium': None,
    'low': 'prio:low',
}

TICKET_RESOLUTION_TO_ISSUE_LABEL = {
    'fixed': 'closed:fixed',
    'invalid': 'closed:invalid',
    'wontfix': 'closed:wontfix',
    'duplicate': 'closed:duplicate',
    'worksforme': 'closed:worksforme',
}

TICKET_STATE_TO_ISSUE_STATE = {
    'new': 'opened',
    'assigned': 'opened',
    'accepted': 'opened',
    'reopened': 'opened',
    'closed': 'closed',
}

ticket_iid=0
milestone_iid=0

################################################################################
# Wiki format normalization
################################################################################

CHANGESET_REX = re.compile(
    r'(?sm)In \[changeset:"([^"/]+?)(?:/[^"]+)?"\]:\n\{\{\{(\n#![^\n]+)?\n(.*?)\n\}\}\}'
)

CHANGESET2_REX = re.compile(
    r'\[changeset:([a-zA-Z0-9]+)\]'
)


def _format_changeset_comment(rex):
    return 'In changeset ' + rex.group(1) + ':\n> ' + rex.group(3).replace('\n', '\n> ')

def _wikiconvert(text, basepath, multiline=True, note_map={}, attachments_path=None, svn2git_revisions={}, gitlab_ref="_todo_"):
    return trac2down.convert(_wikifix(text), basepath, multiline, note_map=note_map, attachments_path=attachments_path, svn2git_revisions=svn2git_revisions)

def _wikifix(text):
    text = CHANGESET_REX.sub(_format_changeset_comment, text)
    text = CHANGESET2_REX.sub(r'\1', text)
    return text

################################################################################
# Trac ticket metadata conversion
################################################################################

def ticket_priority(ticket, priority_to_label=None):
    priority_to_label = priority_to_label or TICKET_PRIORITY_TO_ISSUE_LABEL
    priority = ticket['attributes']['priority']
    if priority in priority_to_label:
        return {priority_to_label[priority]}
    else:
        return set()

def ticket_resolution(ticket, resolution_to_label=None):
    resolution_to_label = resolution_to_label or TICKET_RESOLUTION_TO_ISSUE_LABEL
    resolution = ticket['attributes']['resolution']
    if resolution in resolution_to_label:
        return {resolution_to_label[resolution]}
    else:
        return set()

def ticket_version(ticket):
    try:
        version = ticket['attributes']['version']
    except KeyError:
        return set()

    if version:
        return {'ver:{}'.format(version)}
    else:
        return set()

def ticket_components(ticket):
    components = ticket['attributes']['component'].split(',')
    return {'comp:{}'.format(comp.strip()) for comp in components}

def ticket_note_labels(ticket):
    labels = set()

    for change in ticket['changelog']:
        if not change['field'] in ['resolution', 'status']:
            continue

        if change['field'] == 'resolution':
            if change['newvalue'] == '':
                label = gitlab_resolution_label(change['oldvalue'])
                labels.add(label)
            else:
                label = gitlab_resolution_label(change['newvalue'])
                labels.add(label)

        if change['field'] == 'status':
            label = gitlab_status_label(change['oldvalue'])
            labels.add(label)
            label = gitlab_status_label(change['newvalue'])
            labels.add(label)

    return labels

def ticket_type(ticket):
    ttype = ticket['attributes']['type']
    return {'type:{}'.format(ttype.strip())}

def gitlab_resolution_label(resolution, resolution_to_label=None):
    resolution_to_label = resolution_to_label or TICKET_RESOLUTION_TO_ISSUE_LABEL
    if resolution in resolution_to_label:
        return resolution_to_label[resolution]
    else:
        # meaningful default value for unknown resolutions
        return "closed:fixed"

def gitlab_status_label(status, status_to_state=None):
    status_to_state = status_to_state or TICKET_STATE_TO_ISSUE_STATE
    if status in status_to_state:
        return status_to_state[status]
    else:
        # meaningful default value for unknown statuses
        return "opened"

# https://stackoverflow.com/a/21790513
# https://stackoverflow.com/a/22043027
def render_text_diff(text1, text2):
    diff = difflib.ndiff(text1.splitlines(), text2.splitlines())
    return "```diff\n%s\n```\n" % "\n".join(diff)

# https://gitlab.com/gitlab-org/gitlab-ce/commit/00688e438c04e622a8afb96904b8724f8818f6ce#note_40208591
def render_html5_details(text, summary="Summary"):
    return """
<details>
<summary>%s</summary>
<pre>
%s
</pre>
</details>
""" % (summary, text)

################################################################################
# Trac dict -> GitLab dict conversion
# The GitLab dict is a GitLab model-friendly representation, the GitLab dict
# can be unrolled as kwargs to the corresponding database model entity
# e.g.:
#  dbmodel.Milestone(**milestone_kwargs(trac_milestone))
################################################################################

def identity_converter(value):
    return value

def format_label(value):
    return '~"%s"' % value

def format_emphasis(value):
    return '*%s*' % value

def format_milestone(value):
    return '%%"%s"' % value

def format_fieldchange(field_name, change, value_converter=identity_converter, format_converter=format_emphasis):
    change['field_name'] = field_name

    def converter(value):
        return format_converter(value_converter(value))

    if change['newvalue'] == '':
        change['oldvalue'] = converter(change['oldvalue'])
        note = '- **%(field_name)s** %(oldvalue)s deleted' % change
    elif change['oldvalue'] != '':
        change['oldvalue'] = converter(change['oldvalue'])
        change['newvalue'] = converter(change['newvalue'])
        note = '- **%(field_name)s** changed from %(oldvalue)s to %(newvalue)s' % change
    else:
        change['newvalue'] = converter(change['newvalue'])
        note = '- **%(field_name)s** set to %(newvalue)s' % change

    return note

timetracking_re = re.compile(r"""
    \[/hours/(?P<ticket>\d+)\t(?P<hours>[\d.]+)\thours\]\tlogged\tfor\t(?P<login>\S+):\t''(?P<message>.+)''
""", re.X)

def timetracking_update(text, usermanager):
    """
    u'[/hours/59\t2.2\thours]\tlogged\tfor\tsome-user:\t_some\tmessage\there_',

    Ideally, it should be real timetracking event:
    Elan Ruusamäe @glen added 1h 3m of time spent at 2018-04-02 less than a minute ago
    """
    m = timetracking_re.match(text)
    if not m:
        return None

    d = m.groupdict()

    # message whitespace is tab separated, change to spaces
    d['message'] = d['message'].replace("\t", " ")

    d['user'] = usermanager.get_login(d['login'], d['login'])

    return "%(hours)s hours logged by @%(user)s: _%(message)s_" % d

def format_change_note(change, issue_id=None, note_map={}, svn2git_revisions={}, usermanager=None):
    """
    format "note" for change
    """

    attachments_path = '/uploads/issue_%s' % issue_id
    field = change['field']

    if field == 'comment':
        note = _wikiconvert(change['newvalue'], '/issues/', multiline=False, note_map=note_map,
                            attachments_path=attachments_path, svn2git_revisions=svn2git_revisions)
    elif field == 'resolution':
        note = format_fieldchange('Resolution', change, format_converter=format_label)
    elif field == 'priority':
        note = format_fieldchange('Priority', change, format_converter=format_label)
    elif field == 'milestone':
        note = format_fieldchange('Milestone', change, format_converter=format_milestone)
    elif field == 'estimatedhours':
        note = format_fieldchange('Estimated Hours', change)
    elif field == 'summary':
        note = format_fieldchange('Summary', change)
    elif field == 'status':
        if STATUS_AS_LABEL:
            note = format_fieldchange('Status', change, value_converter=gitlab_status_label, format_converter=format_label)
        else:
            note = format_fieldchange('Status', change, format_converter=format_emphasis)

    elif field == 'version':
        note = format_fieldchange('Version', change, format_converter=format_label)
    elif field == 'description':
        if change['oldvalue'] == '':
            # XXX: does this happen or we need only 'diff' render?
            note = '- **Description** changed\n\n%s' % render_html5_details(change['newvalue'])
        else:
            note = '- **Description** changed\n\n%s' % render_text_diff(change['oldvalue'], change['newvalue'])
    elif field == 'attachment':
        # ![20170905_134928](/uploads/f38feb8a3dc4c5bcabdc41ccc5894ac3/20170905_134928.jpg)
        # will be saved  relative to the project:
        # /var/opt/gitlab/gitlab-rails/uploads/glen/photoproject/f38feb8a3dc4c5bcabdc41ccc5894ac3
        note = '- **Attachment** [%s](%s/%s) added' % (change['newvalue'], attachments_path, change['newvalue'])
    elif field == 'cc':
        note = ''
        for value in change['newvalue'].split(','):
            value = value.strip()
            if value == '':
                continue

            user = usermanager.get_login(value, value)
            if not note:
                note = '- **Cc** added @%s' % user
            else:
                note += ', @%s' % user
        
        if not note:
            LOG.error('Unexpected empty value for %s' % field)
            return ''
    elif field == 'owner':
        if change['oldvalue'] == '' and change['newvalue'] == '':
            # XXX no idea why such changes even exist
            note = ''
        elif change['newvalue'] == '':
            LOG.error('Unexpected empty value for %s' % field)
            return ''

        user = usermanager.get_login(change['newvalue'], change['newvalue'])
        note = '- **Owner** set to @%s' % user
    else:
        raise Exception('Unexpected field %s' % field)

    # ensure we do not yield empty note
    # https://gitlab.com/gitlab-org/gitlab-ce/issues/40297#note_47872749
    return note.strip()

def change_comment_kwargs(change, note):
    """
    called for change['field'] == 'comment'
    """

    return {
        'note': note,
        'created_at': change['time'],
        'updated_at': change['time'],
        # References:
        'author': change['author'],
        'updated_by': change['author'],
        # 'project'
    }

def ticket_kwargs(ticket, ticket_iid, url_files, markdown_files, svn2git_revisions={}):
    LOG.debug('markdown files: %s', markdown_files)
    priority_labels = ticket_priority(ticket)
    resolution_labels = ticket_resolution(ticket)
    version_labels = ticket_version(ticket)
    component_labels = ticket_components(ticket)
    type_labels = ticket_type(ticket)
    state, s = ticket_state(ticket)
    state_labels = set()
    state_labels.add(s)
    note_labels = ticket_note_labels(ticket)

    labels = priority_labels | resolution_labels | version_labels | \
        component_labels | type_labels | state_labels | note_labels

    gitlab_ref = 'issue_'+str(ticket_iid)
    desc = _wikiconvert(ticket['attributes']['description'],
                                    '/issues/', multiline=False, svn2git_revisions=svn2git_revisions, gitlab_ref=gitlab_ref)

    desc += "\n\n"
    uploads = {}
    for file_id in ticket['attachments']:
        info = ticket['attachments'][file_id]
        name = info['attributes']['filename']
        hash = info['data']
        # with open(os.path.join(attachments_path, hash), 'r') as f:
        #     data = f.read()
        #     info['data'] = data
        #     f.close()
        uploads[hash] = info
        desc = gitlab_api.update_desc(desc, name, url_files[name], markdown_files[name])
        #desc += '* %s' % (markdown_files[name][1:])
        #desc += '* [%s](/uploads/issue_%s/%s)\n' % (name, ticket_iid, name)
    
    return {
        'title': ticket['attributes']['summary'],
        'description': desc,
        'state': state,
        'labels': ','.join(labels),
        'created_at': ticket['attributes']['time'],
        'updated_at': ticket['attributes']['changetime'],
        # References:
        'assignee': ticket['attributes']['owner'],
        'author': ticket['attributes']['reporter'],
        'milestone': ticket['attributes']['milestone'],
        # 'project': None,
        'iid': ticket_iid,
        'uploads': uploads,
    }

def milestone_kwargs(milestone):
    global milestone_iid
    milestone_iid = milestone_iid+1

    return {
        'description': _wikiconvert(milestone['description'], '/milestones/', multiline=False),
        'title': milestone['name'],
        # keep open, close later, to handle milestones being referred from comments
#        'state': 'closed' if milestone['completed'] else 'active',
        'state': 'active',
        'due_date': milestone['due'] if milestone['due'] else None,
        # References:
        # 'project': None,
        'iid': milestone_iid,
    }


################################################################################
# Conversion API
################################################################################

def sort_changelog(changelog):
    # Even if the fields and comment have identical date, trac just formats them separately:
    # - fields
    # - comments
    # https://trac.edgewall.org/browser/tags/trac-1.0.15/trac/ticket/templates/ticket_change.html
    # {'author', 'field', 'newvalue', 'oldvalue', 'permanent', 'time'}
    # so sort by date and then items by field being comment
    return sorted(changelog, key = lambda obj: (obj['time'], 1 if obj['field'] == 'comment' else -1, obj['time']))

def merge_changelog(ticket_id, changelog, usermanager):
    """
    Merge changes of type 'resolution' and 'status' into 'comment', because this is how Trac displays changes.

    Basically each 'comment' starts new post, otherwise grouped to same item.
    """

    notes = []
    last_change = None

    def insert_notes(change, notes):
        # first merge notes into
        if len(notes):
            # prepend existing notes
            change['newvalue'] = "\n".join(notes) + "\n\n" + change['newvalue']
        return change

    for change in sort_changelog(changelog):
        last_change = change
        if change['field'] in ['resolution', 'status', 'milestone', 'version', 'description', 'attachment', 'cc', 'summary', 'owner', 'estimatedhours', 'priority']:
            # just collect 'note', the rest is same anyway
            note = format_change_note(change, issue_id=ticket_id, usermanager=usermanager)
            if note == '':
                LOG.info('skip empty comment: change: %r', change)
                continue
            notes.append(note)
            continue

        if change['field'] == 'comment':
            # comment flushes
            # field type comment flushes
            yield insert_notes(change, notes)
            notes = []
            continue

        LOG.info('changelog: skip field: %s', change['field'])

    # last non-comments may need to be flushed as well
    if len(notes):
        yield insert_notes(last_change, notes)

def ticket_state(ticket):
    status_to_state = LabelStatus.MAPPING
    state = ticket['attributes']['status']
    state_label = status_to_state[state]
    return state, state_label

def update_timetracking(issue_args, ticket):
    def convert(hours):
        seconds = float(hours) * 60 * 60
        return seconds

    # timelogs
    if 'totalhours' in ticket['attributes']:
        issue_args['time_spent'] = convert(ticket['attributes']['totalhours'])
    # issue.time_estimate
    if 'estimatedhours' in ticket['attributes']:
        issue_args['time_estimate'] = convert(ticket['attributes']['estimatedhours'])

def migrate_tickets(trac_tickets, gitlab, default_user, usermap, attachments_path, gitlab_project_name, api_project, svn2git_revisions={}, labelmanager=None, usermanager=None):
    LOG.info('MIGRATING %d tickets to issues', len(trac_tickets))
    if len(trac_tickets) == 0:
        return
    for ticket_id, ticket in six.iteritems(trac_tickets):
        LOG.info('migrate #{}: {}'.format(ticket_id, ticket))
        # trac note_id -> gitlab note_id
        note_map = {}
        trac_note_id = 1

        # migrate attachments from comments
        url_files = {}
        markdown_files = {}
        for filename, attachment in six.iteritems(ticket['attachments']):
            attrs = attachment['attributes']
            LOG.info('saving attachment: %s (%d bytes) author: %s, description: %s' % (filename, attrs['size'], attrs['author'], attrs['description']))
            #try:
                #gitlab.save_attachment('issue_%s/%s' % (ticket_id, filename), attachment['data'])
            file_url, file_markdown = gitlab_api.save_attachment(api_project, filename, os.path.join(attachments_path, attachment['data']))
            url_files[filename] = file_url
            markdown_files[filename] = file_markdown
            #except:
            #    pass

        issue_args = ticket_kwargs(ticket, ticket_id, url_files, markdown_files, svn2git_revisions=svn2git_revisions)
        label_set = ticket['labels']
        if STATUS_AS_LABEL:
            issue_args['state'] = label_set.get_status_label().title
        else:
            issue_args['state'] = ticket_state(ticket)

        issue_args['labels'] = ','.join(label_set.get_label_titles())
        issue_args['author'] = usermanager.get_email(issue_args['author'])
        issue_args['assignee'] = usermanager.get_email(issue_args['assignee'])
        issue_args['gitlab_project_name'] = gitlab_project_name

        # Create
        gitlab_issue_id = gitlab.create_issue(**issue_args)
        LOG.info('migrated ticket %s -> %s', ticket_id, gitlab_issue_id)

        # Migrate whole changelog
        LOG.info('changelog: %r', ticket['changelog'])
        for change in merge_changelog(ticket_id, ticket['changelog'], usermanager):
            if change['field'] == 'comment':
                note = timetracking_update(change['newvalue'], usermanager)
                if note is None:
                    note = format_change_note(change, note_map=note_map, issue_id=ticket_id, svn2git_revisions=svn2git_revisions, usermanager=usermanager)
                if note == '':
                    LOG.info('skip empty comment: change: %r', change)
                    continue
                note_args = change_comment_kwargs(change, note)
                note_args['author'] = usermanager.get_email(note_args['author'])
                note_args['updated_by'] = usermanager.get_email(note_args['updated_by'])
                # TODO changelog binary attachments
                gitlab_note_id = gitlab.comment_issue( issue_id=gitlab_issue_id, binary_attachment=None, **note_args)
                LOG.info('migrated ticket #%s note: %r', ticket_id, gitlab_note_id)

                note_map[trac_note_id] = gitlab_note_id
                trac_note_id += 1
            else:
                # TODO: skip field: description
                # skip field: _comment0
                # skip field: component
                # skip field: parents
                # skip field: type
                LOG.info('skip field: %s', change['field'])
        
        # close closed tickets
        for state in issue_args['state']:
            LOG.debug('issue state is %s', state)
            if state == 'closed':
                gitlab_api.update_issue_state(api_project, ticket_id, 'close')



# for ticket comments to appear normally, we created all milestones as 'active'
# now close the milestones that are 'closed'
def close_milestones(trac_milestones, gitlab):
    closed_milestones = [milestone for milestone in six.itervalues(trac_milestones) if milestone['completed']]
    LOG.info('closing %d milestones', len(closed_milestones))
    for milestone in closed_milestones:
        milestone_id = gitlab.get_milestone_id(milestone['name'])
        gitlab.close_milestone(milestone_id)


def migrate_milestones(trac_milestones, gitlab):
    LOG.info('migrating %d milestones', len(trac_milestones))
    for title, milestone in six.iteritems(trac_milestones):
        milestone_args = milestone_kwargs(milestone)
        LOG.info('migrate milestone %r', milestone_args)
        gitlab_milestone_id = gitlab.create_milestone(**milestone_args)
        LOG.info('migrated milestone %s -> %s', title, gitlab_milestone_id)

def migrate_wiki(trac_wiki, gitlab, output_dir, api_project):
    if len(trac_wiki) == 0:
        return
    for title, wiki in six.iteritems(trac_wiki):
        page = wiki['page']
        attachments = wiki['attachments']
        author = wiki['attributes']['author']
        version = wiki['attributes']['version']
        last_modified = wiki['attributes']['lastModified']
        if title == 'WikiStart':
            title = 'home'
        converted_page = trac2down.convert(page, os.path.dirname('/wikis/%s' % title))
        orphaned = []
        for filename, data in six.iteritems(attachments):
            name = filename.split('/')[-1]
            gitlab.save_attachment(name, data)
            converted_page = \
                converted_page.replace(r'migrated/%s)' % filename,
                                       r'migrated/%s)' % name)
            if '%s)' % name not in converted_page:
                orphaned.append(name)
            LOG.debug('migrated attachment %s @ %s', title, filename)
        # Add orphaned attachments to page
        if orphaned:
            converted_page += '\n\n'
            converted_page += '''
##### Orphaned attachments
##### These are the attachments files found but with no references
##### in the page contents.
##### During migration the following orphaned attachments have been found:
'''
            for filename in orphaned:
                converted_page += '- [%s](/uploads/migrated/%s)\n' % (filename, filename)
        # Writeout!
        gitlab_api.save_wiki(api_project, converted_page, title, version, last_modified, author)
        LOG.debug('migrated wiki page %s', title)

# pylint: disable=too-many-arguments
def generate_password(length=None):
    alphabet = string.ascii_letters + string.digits + string.punctuation
    return ''.join(random.choice(alphabet) for _ in range(length or 30))

def create_user(gitlab, email, attributes=None):
    attributes = attributes or {}
    attrs = {  # set mandatory values to defaults
        'email': email,
        'name': email,
        'username': email.split('@')[0],
        'encrypted_password': generate_password(),
    }
    attrs.update(attributes)
    gitlab.create_user(**attrs)

# pylint: disable=too-many-arguments
def migrate(trac, gitlab_project_name, gitlab_version, gitlab_db_connector,
            output_wiki_path, attachments_path, output_uploads_path, gitlab_fallback_user,
            gitlab_private_key=None, gitlab_api_url=None, usermap=None, userattrs=None, svn2git_revisions={}):
    LOG.info('migrating project %r to GitLab ver. %s', gitlab_project_name, gitlab_version)
    LOG.info('uploads repository path is: %r', output_uploads_path)
    db_model = model.get_model(gitlab_version)
    LOG.info('retrieved database model for GitLab ver. %s: %r', gitlab_version, db_model.__file__)
    gitlab = direct.Connection(gitlab_project_name, db_model, gitlab_db_connector,
                               output_uploads_path, create_missing=False)
    LOG.info('estabilished connection to GitLab database')
    # 0. Users
    create_users = False
    usermanager = UserManager(gitlab, usermap=usermap, userattrs=userattrs, fallback_user=gitlab_fallback_user, create_users=create_users)
    usermanager.load_users(trac['authors'])

    # 1. Connect to GitLab API
    api = gitlab_api.connect(gitlab_api_url, gitlab_private_key=gitlab_private_key)
    api_project = gitlab_api.get_project_by_name(api, gitlab_project_name)

    # 2. Clean-up
    # if overwite and mode == direct
    # TODO: make configurable
    gitlab.clear_issues()
    gitlab.clear_milestones()
    gitlab.clear_labels()
    gitlab_api.clear_wiki(api_project)

    # 3. Wiki
    LOG.info('migrating %d wiki pages to: %s', len(trac['wiki']), output_wiki_path)
    migrate_wiki(trac['wiki'], gitlab, output_wiki_path, api_project)

    # 4. Milestones
    migrate_milestones(trac['milestones'], gitlab)
    close_milestones(trac['milestones'], gitlab)

    # 5. create labels
    labelmanager = LabelManager(gitlab, LOG)
    labelmanager.create_labels(trac['tickets'])

    # 6. Issues
    LOG.info('migrating %d tickets to issues', len(trac['tickets']))
    migrate_tickets(trac['tickets'], gitlab, gitlab_fallback_user, usermap,
                        attachments_path, gitlab_project_name, api_project,
                        svn2git_revisions=svn2git_revisions,
                        labelmanager=labelmanager, usermanager=usermanager)

    # 7. Farewell
    LOG.info('done migration of project %r to GitLab ver. %s', gitlab_project_name, gitlab_version)
