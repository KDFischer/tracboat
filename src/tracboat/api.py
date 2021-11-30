# -*- coding: utf-8 -*-

import gitlab
import logging
import re

from gitlab.exceptions import GitlabGetError

__all__ = ['api']

LOG = logging.getLogger(__name__)

def connect(gitlab_api_url, gitlab_private_key=None):
    LOG.info('GitLab API URL: %s', gitlab_api_url)
    if  gitlab_private_key:
        LOG.info('GitLab private key is set')
    else:
        LOG.warning('GitLab private key is NOT set!')
    try:
        gl = gitlab.Gitlab(gitlab_api_url, private_token=gitlab_private_key)
    except GitlabHttpError:
        LOG.error('Connecting to GitLab API failed')
    return gl

def get_project_by_name(gitlab, name):
    LOG.debug('Getting GitLab project %s', name)
    try:
        project = gitlab.projects.get(name)
    except GitlabGetError:
        LOG.error('GitLab project %s not found.', name)
    return project

# wiki
def clear_wiki(project):
    LOG.warning('Deleting all wiki pages from %s.', project.name)
    pages = project.wikis.list()
    for page in pages:
        page.delete()

def save_wiki(project, converted_page, title, version, last_modified, author):
    page = project.wikis.create({
        'title': title,
        'content': converted_page,
    })
    LOG.debug('Created wiki page {} as {}.'.format(page.title, page.slug))

# issues
def update_issue_state(project, issue_iid, state):
    issue = project.issues.get(issue_iid)
    issue.state_event = state
    issue.save()


def save_attachment(project, filename, filepath):
    LOG.debug('Project %s', project.name)
    LOG.debug('Filename: %s', filename)
    LOG.debug('Filepath: %s', filepath)
    uploaded_file = project.upload(filename, filepath=filepath)
    return uploaded_file['url'], uploaded_file['markdown']

def update_desc(desc, name, url, markdown):
    # update url
    pattern = r"({0})(.*)(/uploads/{0})(.*\))".format(name)
    repl = r"\1]({0})".format(url)
    LOG.debug('search pattern: %s', pattern)
    LOG.debug('replacement string %s', repl)
    desc = re.sub(pattern, repl, desc)

    # add list of files to the end
    if markdown[0] == '!':
        desc += '* %s\n' % (markdown[1:])
    else:
        desc += '* %s\n' % (markdown)
    return desc
    