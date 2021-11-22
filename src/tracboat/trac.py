# -*- coding: utf-8 -*-

import logging
import ssl
import hashlib
from os import path

import six
from six.moves import xmlrpc_client as xmlrpc

LOG = logging.getLogger(__name__)


def _safe_retrieve_data(data, encoding='base64'):
    try:
        # six.b(data.decode(encoding))
        return six.b(data)
    except Exception as err:  # pylint: disable=broad-except
        LOG.exception('error while decoding data from %s', encoding)
        return str(err)


def _authors_collect(wiki=None, tickets=None):
    wiki = wiki or {}
    tickets = tickets or {}
    return list(set(
        [page['attributes']['author'] for page in six.itervalues(wiki)] +
        [ticket['attributes']['reporter'] for ticket in six.itervalues(tickets)] +
        [ticket['attributes']['owner'] for ticket in six.itervalues(tickets)] +
        [change['author'] for ticket in six.itervalues(tickets) for change in ticket['changelog']] +
        [change['newvalue'] for ticket in six.itervalues(tickets) for change in ticket['changelog'] if change['field'] == 'owner'] +
        [value.strip() for ticket in six.itervalues(tickets) for change in ticket['changelog'] if change['field'] == 'cc' for value in change['newvalue'].split(',')]
    ))


def ticket_get_attributes(source, ticket_id):
    LOG.debug('ticket_get_attributes of ticket #%s', ticket_id)
    ticket = source.ticket.get(ticket_id)
    return ticket[3]


def ticket_get_changelog(source, ticket_id):
    LOG.debug('ticket_get_changelog of ticket #%s', ticket_id)
    # the results are ordered by time,permanent,author tuple
    # https://trac.edgewall.org/browser/tags/trac-1.0.15/trac/ticket/model.py#L383
    try:
        return [
            {
                'time': c[0],
                'author': c[1],
                'field': c[2],
                'oldvalue': c[3],
                'newvalue': c[4],
                'permanent': bool(c[5])
            }
            for c in source.ticket.changeLog(ticket_id)
        ]
    except Exception as ex:
        LOG.error("Could not retrive changelog on ticket {}, error: {}".format(ticket_id, ex))
        return []



def ticket_get_attachments(source, ticket_id, attachments_path):
    
    LOG.debug('ticket_get_attachments of ticket #%s', ticket_id)
    ret = {}
    try:
        als = source.ticket.listAttachments(ticket_id)
    except:
        try:
            als = source.ticket.listAttachments(ticket_id)
        except:
            als = []
    for meta in als:
        try:
            data = _safe_retrieve_data(source.ticket.getAttachment(ticket_id, meta[0]).data)
        except:
            try:
                data = _safe_retrieve_data(source.ticket.getAttachment(ticket_id, meta[0]).data)
            except:
                data = ''
        hash = hashlib.md5(data).hexdigest()
        with open(path.join(attachments_path, hash), 'w') as f:
            f.write(data)
            f.close()
        ret[meta[0]] =  {
            'attributes': {
                'filename': meta[0],
                'description': meta[1],
                'size': meta[2],
                'time': meta[3],
                'author': meta[4],
            },
            'data': hash
        }
    return ret


def ticket_get_all(source, attachments_path, attachments=True):
    LOG.debug('ticket_get_all')
    return {
        ticket_id: {
            'attributes': ticket_get_attributes(source, ticket_id),
            'changelog': ticket_get_changelog(source, ticket_id),
            'attachments': ticket_get_attachments(source, ticket_id, attachments_path) if attachments else {},
        }
        for ticket_id in source.ticket.query("max=0&order=id")
    }


def milestone_get_all(source):
    LOG.debug('milestone_get_all')
    return {
        milestone: source.ticket.milestone.get(milestone)
        for milestone in milestone_get_all_names(source)
    }


def milestone_get(source, milestone_name):
    LOG.debug('milestone_get of milestone %s', milestone_name)
    return source.ticket.milestone.get(milestone_name)


def milestone_get_all_names(source):
    LOG.debug('milestone_get_all_names')
    return list(source.ticket.milestone.getAll())


def wiki_get_all_pages(source, authors_blacklist=None, contents=True, attachments=True,
                       exclude_system_pages=True):
    LOG.debug('wiki_get_all_pages')
    authors_blacklist = set(authors_blacklist or [])
    if exclude_system_pages:
        authors_blacklist.add('trac')
    LOG.debug('wiki_get_all_pages is retrieving metadata for all pages')
    pages = {
        name: {
            'attributes': source.wiki.getPageInfo(name),
            'page': '',
            'attachments': {},
        }
        for name in source.wiki.getAllPages()
    }
    if authors_blacklist:
        LOG.debug('wiki_get_all_pages is blacklisting authors: %s', authors_blacklist)
        pages = {
            k: v for k, v in six.iteritems(pages)
            if v['attributes']['author'] not in authors_blacklist
        }
    if contents:
        for pagename, pagedict in six.iteritems(pages):
            LOG.debug('wiki_get_all_pages is retrieving contents for wiki page %s', pagename)
            pagedict['page'] = source.wiki.getPage(pagename)
    if attachments:
        for pagename, pagedict in six.iteritems(pages):
            LOG.debug('wiki_get_all_pages is retrieving attachments for wiki page %s', pagename)
            pagedict['attachments'] = {} #{
            #    filename: _safe_retrieve_data(source.wiki.getAttachment(filename).data)
            #    for filename in source.wiki.listAttachments(pagename)
            #}
    return pages


def project_get(source, attachments_path=None, collect_authors=True):
    LOG.debug('project_get')
    project = {
        'wiki': wiki_get_all_pages(source),
        'tickets': ticket_get_all(source, attachments_path),
        'milestones': milestone_get_all(source),
        'authors': [],
    }
    if collect_authors:
        LOG.debug('project_get is collecting authors from project')
        project['authors'] = _authors_collect(wiki=project['wiki'], tickets=project['tickets'])
    return project


def authors_get(source, from_wiki=True, from_tickets=True):
    wiki = wiki_get_all_pages(source, contents=False, attachments=False) if from_wiki else None
    tickets = ticket_get_all(source, attachments=False) if from_tickets else None
    return _authors_collect(wiki=wiki, tickets=tickets)


def connect(url, encoding='UTF-8', use_datetime=True, ssl_verify=True):
    # pylint: disable=protected-access
    context = None if ssl_verify else ssl._create_unverified_context()
    return xmlrpc.ServerProxy(url, encoding=encoding, use_datetime=use_datetime, context=context)
