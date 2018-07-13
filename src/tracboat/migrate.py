# -*- coding: utf-8 -*-

import logging
import os
import random
import re
import string
from os import path
from itertools import chain

import six

from tracboat import trac2down
from tracboat.gitlab import direct  # TODO selectable mode (api/direct)
from tracboat.gitlab import model

__all__ = ['migrate']

LOG = logging.getLogger(__name__)

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


def _wikifix(text):
    text = CHANGESET_REX.sub(_format_changeset_comment, text)
    text = CHANGESET2_REX.sub(r'\1', text)
    return text


def _wikiconvert(text, basepath, multiline=True, gitlab_ref="_todo_"):
    return trac2down.convert(_wikifix(text), basepath, multiline, gitlab_ref)


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


def gitlab_resolution_label(resolution, resolution_to_label=None):
    resolution_to_label = resolution_to_label or TICKET_RESOLUTION_TO_ISSUE_LABEL
    if resolution in resolution_to_label:
        return resolution_to_label[resolution]
    else:
        # meaningful default value for unknown resolutions
        return "closed:fixed"


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


def gitlab_status_label(status, status_to_state=None):
    status_to_state = status_to_state or TICKET_STATE_TO_ISSUE_STATE
    if status in status_to_state:
        return status_to_state[status]
    else:
        # meaningful default value for unknown statuses
        return "opened"


def ticket_state(ticket, status_to_state=None):
    status_to_state = status_to_state or TICKET_STATE_TO_ISSUE_STATE
    state = ticket['attributes']['status']
    if state in status_to_state:
        return status_to_state[state], set()
    else:
        return "opened", set()


################################################################################
# Trac dict -> GitLab dict conversion
# The GitLab dict is a GitLab model-friendly representation, the GitLab dict
# can be unrolled as kwargs to the corresponding database model entity
# e.g.:
#  dbmodel.Milestone(**milestone_kwargs(trac_milestone))
################################################################################

def change_kwargs(change):
    if change['field'] == 'comment':
        note = _wikiconvert(change['newvalue'], '/issues/', multiline=False)
    elif change['field'] == 'resolution':
        if change['newvalue'] == '':
            resolution = gitlab_resolution_label(change['oldvalue'])
            note = '**Resolution** ~"%s" deleted' % resolution
        else:
            resolution = gitlab_resolution_label(change['newvalue'])
            note = '**Resolution** set to ~"%s"' % resolution
    elif change['field'] == 'status':
        oldstatus = gitlab_status_label(change['oldvalue'])
        newstatus = gitlab_status_label(change['newvalue'])
        note = "**Status** changed from *%s* to *%s*" % (oldstatus, newstatus)
    else:
        raise Exception('Unexpected field %s' % change['field'])

    return {
        'note': note,
        'created_at': change['time'],
        'updated_at': change['time'],
        # References:
        'author': change['author'],
        'updated_by': change['author'],
        # 'project'
    }

ticket_iid=0

def ticket_kwargs(ticket, ticket_iid, attachments_path):
    priority_labels = ticket_priority(ticket)
    resolution_labels = ticket_resolution(ticket)
    version_labels = ticket_version(ticket)
    component_labels = ticket_components(ticket)
    type_labels = ticket_type(ticket)
    state, state_labels = ticket_state(ticket)
    #global ticket_iid
    #ticket_iid = ticket_iid+1
    note_labels = ticket_note_labels(ticket)

    labels = priority_labels | resolution_labels | version_labels | \
        component_labels | type_labels | state_labels | note_labels

    gitlab_ref = 'issue_'+str(ticket_iid)
    desc = _wikiconvert(ticket['attributes']['description'],
                                    '/issues/', multiline=False, gitlab_ref=gitlab_ref)

    desc += "\n\n"
    uploads = {}
    for file_id in ticket['attachments']:
        info = ticket['attachments'][file_id]
        name = info['attributes']['filename']
        hash = info['data']
        with open(path.join(attachments_path, hash), 'r') as f:
            data = f.read()
            info['data'] = data
            f.close()
        uploads[hash] = info
        desc += '* [%s](/uploads/issue_%s/%s)\n' % (name, ticket_iid, name)

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

milestone_iid=0

def milestone_kwargs(milestone):
    global milestone_iid
    milestone_iid = milestone_iid+1

    return {
        'description': _wikiconvert(milestone['description'], '/milestones/', multiline=False),
        'title': milestone['name'],
        'state': 'closed' if milestone['completed'] else 'active',
        'due_date': milestone['due'] if milestone['due'] else None,
        # References:
        # 'project': None,
        'iid': milestone_iid,
    }


################################################################################
# Conversion API
################################################################################

def migrate_tickets(trac_tickets, gitlab, default_user, usermap, attachments_path, gitlab_project_name):
    for ticket_id, ticket in six.iteritems(trac_tickets):
        issue_args = ticket_kwargs(ticket, ticket_id, attachments_path)
        # Fix user mapping
        issue_args['author'] = usermap.get(issue_args['author'], default_user)
        issue_args['assignee'] = usermap.get(issue_args['assignee'], default_user)
        issue_args['gitlab_project_name'] = gitlab_project_name
        # Create
        gitlab_issue_id = gitlab.create_issue(**issue_args)
        LOG.debug('migrated ticket %s -> %s', ticket_id, gitlab_issue_id)
        # Migrate whole changelog
        for change in ticket['changelog']:
            if change['field'] in ['comment', 'resolution', 'status']:
                note_args = change_kwargs(change)
                if note_args['note'] == '':
                    LOG.info('skip empty comment: %r; change: %r', note_args, change)
                    continue
                # Fix user mapping
                note_args['author'] = usermap.get(note_args['author'], default_user)
                note_args['updated_by'] = usermap.get(note_args['updated_by'], default_user)
                gitlab_note_id = gitlab.comment_issue(
                    # TODO changelog binary attachments
                    issue_id=gitlab_issue_id, binary_attachment=None, **note_args)
                LOG.debug('migrated ticket #%s change -> %s', ticket_id, gitlab_note_id)
        #if ticket_id=="2318":
        #    break


def migrate_milestones(trac_milestones, gitlab):
    for title, milestone in six.iteritems(trac_milestones):
        milestone_args = milestone_kwargs(milestone)
        gitlab_milestone_id = gitlab.create_milestone(**milestone_args)
        LOG.debug('migrated milestone %s -> %s', title, gitlab_milestone_id)


def migrate_wiki(trac_wiki, gitlab, output_dir):
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
            gitlab.save_wiki_attachment(name, data)
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
        trac2down.save_file(converted_page, title, version, last_modified, author, output_dir)
        LOG.debug('migrated wiki page %s', title)


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
            usermap=None, userattrs=None):
    LOG.info('migrating project %r to GitLab ver. %s', gitlab_project_name, gitlab_version)
    LOG.info('uploads repository path is: %r', output_uploads_path)
    db_model = model.get_model(gitlab_version)
    LOG.info('retrieved database model for GitLab ver. %s: %r', gitlab_version, db_model.__file__)
    gitlab = direct.Connection(gitlab_project_name, db_model, gitlab_db_connector,
                               output_uploads_path, create_missing=True)
    LOG.info('estabilished connection to GitLab database')
    # 0. Users
    for email in chain(six.itervalues(usermap), [gitlab_fallback_user]):
        attrs = {  # set mandatory values to defaults
            'email': email,
            'username': email.split('@')[0],
            'encrypted_password': generate_password(),
        }
        attrs.update(userattrs.get(email, {}))
        gitlab.create_user(**attrs)
        LOG.info('created GitLab user %r', email)
        LOG.debug('created GitLab user %r with attributes: %r', email, attrs)
    # 1. Wiki
    LOG.info('migrating %d wiki pages to: %s', len(trac['wiki']), output_wiki_path)
    migrate_wiki(trac['wiki'], gitlab, output_wiki_path)
    # 2. Milestones
    LOG.info('migrating %d milestones', len(trac['milestones']))
    migrate_milestones(trac['milestones'], gitlab)
    # 3. Issues
    LOG.info('migrating %d tickets to issues', len(trac['tickets']))
    migrate_tickets(trac['tickets'], gitlab, gitlab_fallback_user, usermap, attachments_path, gitlab_project_name)
    # Farewell
    LOG.info('done migration of project %r to GitLab ver. %s', gitlab_project_name, gitlab_version)
