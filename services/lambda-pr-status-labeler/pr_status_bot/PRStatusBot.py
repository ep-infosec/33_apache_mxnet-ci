#!/usr/bin/env python3

# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

# -*- coding: utf-8 -*-
import ast
import json
import hmac
import hashlib
import os
import logging
import re
import secret_manager

from github import Github

# Define the constants
# Github labels
PR_WORK_IN_PROGRESS_LABEL = 'pr-work-in-progress'
PR_AWAITING_TESTING_LABEL = 'pr-awaiting-testing'
PR_AWAITING_MERGE_LABEL = 'pr-awaiting-merge'
PR_AWAITING_REVIEW_LABEL = 'pr-awaiting-review'
PR_AWAITING_RESPONSE_LABEL = 'pr-awaiting-response'

WORK_IN_PROGRESS_TITLE_SUBSTRING = 'WIP'

# CI state
FAILURE_STATE = 'failure'
PENDING_STATE = 'pending'
SUCCESS_STATE = 'success'

# Review state
APPROVED_STATE = 'APPROVED'
CHANGES_REQUESTED_STATE = 'CHANGES_REQUESTED'
COMMENTED_STATE = 'COMMENTED'
DISMISSED_STATE = 'DISMISSED'


class GithubObj:
    def __init__(self,
                 github_personal_access_token=None,
                 apply_secret=True):
        """
        Initializes the Github Object
        :param github_personal_access_token: GitHub authentication token (Personal access token)
        :param apply_secret: GitHub secret credential (Secret credential that is unique to a GitHub developer)
        """
        self.github_personal_access_token = github_personal_access_token
        if apply_secret:
            self._get_secret()
        self.github_object = self._get_github_object()

    def _get_github_object(self):
        """
        This method returns github object initialized with Github personal access token
        """
        github_obj = Github(self.github_personal_access_token)
        return github_obj

    def _get_secret(self):
        """
        This method is to get secret value from Secrets Manager
        """
        secret = json.loads(secret_manager.get_secret())
        self.github_personal_access_token = secret["github_personal_access_token"]


class PRStatusBot:
    def __init__(self,
                 repo=os.environ.get("repo"),
                 github_obj=None,
                 apply_secret=True):
        """
        Initializes the PR Status Bot
        :param repo: GitHub repository that is being referenced
        :param apply_secret: GitHub secret credential (Secret credential that is unique to a GitHub developer)
        """
        self.repo = repo
        self.github_obj = github_obj
        self.latest_commit_sha = None
        if apply_secret:
            self._get_secret()

    def _get_secret(self):
        """
        This method is to get secret value from Secrets Manager
        """
        secret = json.loads(secret_manager.get_secret())
        self.webhook_secret = secret["webhook_secret"]

    def _secure_webhook(self, event):
        """
        This method will validate the security of the webhook, it confirms that the secret
        of the webhook is matched and that each github event is signed appropriately
        :param event: The github event we want to validate
        :return Response denoting success or failure of security
        """

        # Validating github event is signed
        try:
            git_signed = ast.literal_eval(event["Records"][0]['body'])['headers']["X-Hub-Signature"]
        except KeyError:
            raise Exception("WebHook from GitHub is not signed")
        git_signed = git_signed.replace('sha1=', '')

        # Signing our event with the same secret as what we assigned to github event
        secret = self.webhook_secret
        body = ast.literal_eval(event["Records"][0]['body'])['body']
        secret_sign = hmac.new(key=secret.encode('utf-8'), msg=body.encode('utf-8'), digestmod=hashlib.sha1).hexdigest()

        # Validating signatures match
        return hmac.compare_digest(git_signed, secret_sign)

    def _get_pull_request_object(self, pr_number):
        """
        This method returns a PullRequest object based on the PR number
        :param pr_number
        """
        repo = self.github_obj.get_repo(self.repo)
        pr_obj = repo.get_pull(int(pr_number))
        return pr_obj

    def _get_commit_object(self, commit_sha):
        """
        This method returns a Commit object based on the SHA of the commit
        :param commit_sha
        """
        repo = self.github_obj.get_repo(self.repo)
        commit_obj = repo.get_commit(commit_sha)
        return commit_obj

    def _is_mxnet_committer(self, reviewer):
        """
        This method checks if the Pull Request reviewer is a member of MXNet committers
        It uses the Github API for fetching team members of a repo
        Only a Committer can access [read/write] to Apache MXNet Committer team on Github
        Retrieved the Team ID of the Apache MXNet Committer team on Github using a Committer's credentials
        """
        team = self.github_obj.get_organization('apache').get_team(2413476)
        return team.has_in_members(reviewer)

    def _drop_other_pr_labels(self, pr, desired_label):
        labels = pr.get_labels()
        if not labels:
            logging.info('No labels found')
            return

        for label in labels:
            logging.info(f'Label:{label}')
            if label.name.startswith('pr-') and label.name != desired_label:
                try:
                    logging.info(f'Removing {label}')
                    pr.remove_from_labels(label)
                except Exception:
                    logging.error(f'Error while removing the label {label}')

    def _add_label(self, pr, label):
        # drop other PR labels
        self._drop_other_pr_labels(pr, label)

        # check if the PR already has the desired label
        if(self._has_desired_label(pr, label)):
            logging.info(f'PR {pr.number} already contains the label {label}')
            return

        logging.info(f'BOT Labels: {label}')
        try:
            pr.add_to_labels(label)
        except Exception:
            logging.error(f'Unable to add label {label}')

        # verify that label has been correctly added
        # if(self._has_desired_label(pr, label)):
        #     logging.info(f'Successfully labeled {label} for PR-{pr.number}')
        return

    def _has_desired_label(self, pr, desired_label):
        """
        This method returns True if desired label found in PR labels
        """
        labels = pr.get_labels()
        for label in labels:
            if desired_label == label.name:
                return True
        return False

    def get_review_counts(self, review, approvers, change_requesters, commenters, dismissed):
        if review.state == APPROVED_STATE:
            approvers.append(review.user.login)
        elif review.state == CHANGES_REQUESTED_STATE:
            change_requesters.append(review.user.login)
        elif review.state == COMMENTED_STATE:
            commenters.append(review.user.login)
        elif review.state == DISMISSED_STATE:
            dismissed.append(review.user.login)
        else:
            logging.error(f'Unknown review state {review.state}')
        return approvers, change_requesters, commenters, dismissed

    def _parse_reviews(self, pr):
        """
        This method parses through the reviews of the PR and returns count of
        3 states: Approved reviews, Comment reviews, Requested Changes reviews

        All these 3 states take into account if there are dismissed reviews.
        Approved review / Requested changes review can be dismissed.
        If dismissed, then the review doesn't count.
        Note: Only reviews by MXNet Committers are considered.
        :param pr
        """
        approvers = []
        change_requesters = []
        commenters = []
        dismissed = []
        for review in pr.get_reviews():
            # continue if the review is for a stale commit
            if(review.commit_id != self.latest_commit_sha):
                continue
            # continue if the review is by non-committer
            reviewer = review.user
            if not self._is_mxnet_committer(reviewer):
                logging.info(f'Review is by non-MXNet Committer: {reviewer}. Ignore.')
                continue
            approvers, change_requesters, commenters, dismissed = self.get_review_counts(review, approvers, change_requesters, commenters, dismissed)
        approvers = list(set(approvers) - set(dismissed))
        change_requesters = list(set(change_requesters) - set(dismissed))
        commenters = list(set(commenters))
        approved_count = len(approvers) if len(approvers) else 0
        requested_changes_count = len(change_requesters) if len(change_requesters) else 0
        comment_count = len(commenters) if len(commenters) else 0
        return approved_count, requested_changes_count, comment_count

    def _label_pr_based_on_status(self, full_build_status_state, pull_request_obj):
        """
        This method checks the CI status of the specific commit of the PR
        and it labels the PR accordingly
        :param full_build_status_state
        :param pull_request_obj
        """
        # pseudo-code
        # if WIP in title or PR is draft or CI failed:
        #   pr-work-in-progress
        # elif CI has not started yet or CI is in progress:
        #   pr-awaiting-testing
        # else: # CI passed checks
        #   if pr has at least one approval and no request changes:
        #       pr-awaiting-merge
        #   elif pr has no review or all reviews have been dismissed/re-requested:
        #       pr-awaiting-review
        #   else: # pr has a review that hasn't been dismissed yet no approval
        #       pr-awaiting-response

        # combined status of PR can be 1 of the 3 potential states
        # https://developer.github.com/v3/repos/statuses/#get-the-combined-status-for-a-specific-reference
        wip_in_title, ci_failed, ci_pending = False, False, False
        if full_build_status_state == FAILURE_STATE:
            ci_failed = True
        elif full_build_status_state == PENDING_STATE:
            ci_pending = True

        if WORK_IN_PROGRESS_TITLE_SUBSTRING in pull_request_obj.title:
            logging.info('WIP in PR Title')
            wip_in_title = True
        work_in_progress_conditions = wip_in_title or pull_request_obj.draft or ci_failed
        if work_in_progress_conditions:
            self._add_label(pull_request_obj, PR_WORK_IN_PROGRESS_LABEL)
        elif ci_pending:
            self._add_label(pull_request_obj, PR_AWAITING_TESTING_LABEL)
        else:  # CI passed since status=successful
            # parse reviews to assess count of approved/requested changes/commented reviews
            # make sure you take into account dismissed reviews
            approves, request_changes, comments = self._parse_reviews(pull_request_obj)
            if approves > 0 and request_changes == 0:
                self._add_label(pull_request_obj, PR_AWAITING_MERGE_LABEL)
            else:
                # decisive review means approve/request change
                # comment is a non-decisive review
                has_no_decisive_reviews = approves + request_changes == 0
                if has_no_decisive_reviews:
                    self._add_label(pull_request_obj, PR_AWAITING_REVIEW_LABEL)
                else:
                    self._add_label(pull_request_obj, PR_AWAITING_RESPONSE_LABEL)
        return

    def _get_latest_commit(self, pull_request_obj):
        """
        This method returns the latest commit of the Pull Request
        :param pull_request_obj
        :returns latest_commit
        """
        latest_commit = pull_request_obj.get_commits()[pull_request_obj.commits - 1]
        return latest_commit

    def _is_stale_commit(self, commit_sha, pull_request_obj):
        """
        This method checks if the given commit is stale or not
        :param commit_sha
        :param pull_request_obj
        :returns boolean
        """
        latest_commit = self._get_latest_commit(pull_request_obj)
        self.latest_commit_sha = latest_commit.sha
        if commit_sha == self.latest_commit_sha:
            logging.info(f'Current commit {commit_sha} is latest commit of PR {pull_request_obj.number}')
            return False
        else:
            logging.info(f'Latest commit of PR {pull_request_obj.number}: {self.latest_commit_sha}')
            logging.info(f'Current status belongs to stale commit {commit_sha}')
            return True

    def _get_full_build_status_from_combined_status(self, commit_obj, combined_status_state):
        """
        Due to staggered build pipelines, combined status isn't reflective of full build status
        i.e. When sanity passes, combined_status_state = Success
        It should be pending since other pipelines aren't successful yet.
        However, combined_status_state only takes into account the pipelines that have been triggered until that point.
        Thus, manually check if combined_status_state and length of combined_statuses
        """
        combined_status_list = commit_obj.get_combined_status().statuses
        combined_status_length = len(combined_status_list)
        logging.info(f'Combined Status Length: {combined_status_length}')
        if combined_status_state == SUCCESS_STATE and combined_status_length == 1:
            # Only sanity build has passed; rest of the builds haven't been triggered
            return PENDING_STATE
        # Full build has been triggered
        return combined_status_state

    def parse_payload(self, payload):
        """
        This method parses the payload and process it according to the event status
        """
        # CI is run for non-PR commits as well
        # for instance, after PR is merged into the master/v1.x branch
        # we exit in such a case
        # to detect if the status update is for a PR commit or a merged commit
        # we rely on Target_URL in the event payload
        # e.g. http//jenkins.mxnet-ci.amazon-ml.com/job/mxnet-validation/job/sanity/job/PR-18899/1/display/redirect
        target_url = payload['target_url']
        if 'PR' not in target_url:
            logging.info('Status update doesnt belong to a PR commit')
            return 1
        # strip PR number from the target URL
        # use raw string instead of normal string to make regex check pep8 compliant
        pr_number = re.search(r"PR-(\d+)", target_url, re.IGNORECASE).group(1)
        logging.info(f'--------- PR : {pr_number} ----------')
        pull_request_obj = self._get_pull_request_object(pr_number)

        # verify PR is open
        # return if PR is closed
        if pull_request_obj.state == 'closed':
            logging.info('PR is closed. No point in labeling')
            return 2

        # CI runs for stale commits
        # return if its status update of a stale commit
        commit_sha = payload['commit']['sha']
        if self._is_stale_commit(commit_sha, pull_request_obj):
            return

        context = payload['context']
        state = payload['state']

        logging.info(f'PR Context: {context}')
        logging.info(f'Context State: {state}')

        commit_obj = self._get_commit_object(commit_sha)
        combined_status_state = commit_obj.get_combined_status().state
        logging.info(f'PR Combined Status State: {combined_status_state}')
        full_build_status_state = self._get_full_build_status_from_combined_status(commit_obj, combined_status_state)
        logging.info(f'PR Full Build Status State: {full_build_status_state}')
        self._label_pr_based_on_status(full_build_status_state, pull_request_obj)

    def parse_webhook_data(self, event):
        """
        This method handles the processing for each PR depending on the appropriate Github event
        information provided by the Github Webhook.
        """
        try:
            github_event = ast.literal_eval(event["Records"][0]['body'])['headers']["X-GitHub-Event"]
            logging.info(f"github event {github_event}")
        except KeyError:
            raise Exception("Not a GitHub Event")

        if not self._secure_webhook(event):
            raise Exception("Failed to validate WebHook security")

        try:
            payload = json.loads(ast.literal_eval(event["Records"][0]['body'])['body'])
        except ValueError:
            raise Exception("Decoding JSON for payload failed")

        self.parse_payload(payload)
