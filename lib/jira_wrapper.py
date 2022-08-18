#!/usr/bin/env python

"""
jira_tickets.py - idempotently copy the issue data from github_tickets.py to issues.redhat.com

The jira instance on issues.redhat.com does have an api, but it's shielded by sso and regular users
can not generate tokens nor do advanced data import. This script works around all of that by using
selenium to navigate through the pages and to input the data.
"""

import copy
import glob
import json
import os
import time
import jira

from pprint import pprint
from logzero import logger


DATA_DIR = 'data'
WAIT_SECONDS = 60


class JiraWrapper:

    errata = None
    bugzillas = None
    jira_issues = None
    cachedir = '.data'
    driver = None

    def __init__(self):

        jira_token = os.environ.get('JIRA_TOKEN')
        if not jira_token:
            raise Exception('JIRA_TOKEN must be set!')
        logger.info('start jira client')
        self.jira_client = jira.JIRA(
            {'server': 'https://issues.redhat.com'},
            token_auth=jira_token
        )

        logger.info('scrape jira issues')
        self.scrape_jira_issues()

        logger.info('save jira issues to disk')
        self.save_data()

    def save_data(self):
        if not os.path.exists(self.cachedir):
            os.makedirs(self.cachedir)
        jfile = os.path.join(self.cachedir, 'jiras.json')
        with open(jfile, 'w') as f:
            f.write(json.dumps(self.jira_issues, indent=2))

    @property
    def issue_map(self):
        imap = {}
        for issue in self.jira_issues:
            key = issue['key']
            number = int(key.replace('AAH-', ''))
            imap[number] = issue
        return imap

    def scrape_jira_issues(self, github_issue_to_find=None):

        def run_search_and_populate_issues(query, maxResults):
            issues = self.jira_client.search_issues(query, maxResults=maxResults)
            for issue in issues:
                inum = int(issue.key.replace('AAH-', ''))
                if inum in self.issue_map:
                    continue
                logger.info(f'{issue.key} {issue.fields.summary}')
                try:
                    self.jira_issues.append(issue.raw)
                except Exception as e:
                    logger.exception(e)
                    continue


        self.jira_issues = []

        qs = 'project = AAH ORDER BY created DESC'

        logger.info('get the newest ticket number')
        issues = self.jira_client.search_issues(qs, maxResults=1)
        latest = issues[0].key
        latest_number = int(latest.replace('AAH-', ''))

        logger.info('get the latest 1000')
        run_search_and_populate_issues(qs, 1000)

        #ikeys = sorted([x for x in list(self.issue_map.keys())])
        #oldest = ikeys[0]
        #import epdb; epdb.st()

        logger.info('get the first 1000')
        run_search_and_populate_issues(qs.replace('DESC', 'ASC'), 1000)

        logger.info('get the missing numbers')
        imap = self.issue_map
        fetched = sorted(list(imap.keys()))
        unfetched = list(range(1, fetched[0]))

        failed = []
        for x in unfetched:
            key = f'AAH-{x}'
            logger.info(f'get {key}')
            try:
                issue = self.jira_client.issue(key)
                self.jira_issues.append(issue.raw)
            except Exception as e:
                logger.exception(e)
                failed.append(key)
                continue
        logger.error(f'total failures: {len(failed)}')


def main():
    jw = JiraWrapper()


if __name__ == "__main__":
    main()
