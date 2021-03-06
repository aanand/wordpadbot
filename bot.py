#!/usr/bin/env python2
# -*- coding: utf-8 -*- #

from __future__ import unicode_literals

from twitterbot import TwitterBot
from tweepy.error import TweepError

from extensions.wordpad import wordpad
from extensions.sql_storage import SQLStorage

import arrow
import requests

import random
import os
import logging
from io import BytesIO


SALUTATIONS = [
    'Hello!',
    'Hello {author.name}!',
    'Thank you!',
    'Thank you, {author.name}!',
    'Fixed it!',
    'Oh no!',
    'WordPad! WordPad.',
    'Is this OK?',
    'Is this OK, {author.name}?',
    'Check it out!',
    'Hahahaha! Haha.',
    'Let\u2019s rock!',
    '(\u25d5\u203f\u25d5\u273f)',
    '\u30fd(*\u30fb\u03c9\u30fb)\uff89',
]


class WordPadBot(TwitterBot):
    def bot_init(self):
        self.config['storage'] = SQLStorage(os.environ['DATABASE_URL'])

        self.config['api_key'] = os.environ['TWITTER_CONSUMER_KEY']
        self.config['api_secret'] = os.environ['TWITTER_CONSUMER_SECRET']
        self.config['access_key'] = os.environ['TWITTER_ACCESS_TOKEN']
        self.config['access_secret'] = os.environ['TWITTER_ACCESS_TOKEN_SECRET']

        # use this to define a (min, max) random range of how often to tweet
        # e.g., self.config['tweet_interval_range'] = (5*60, 10*60) # tweets every 5-10 minutes
        self.config['tweet_interval_range'] = (1*60, 3*60*60)

        # only reply to tweets that specifically mention the bot
        self.config['reply_direct_mention_only'] = False

        # only include bot followers (and original tweeter) in @-replies
        self.config['reply_followers_only'] = False

        # fav any tweets that mention this bot?
        self.config['autofav_mentions'] = False

        # fav any tweets containing these keywords?
        self.config['autofav_keywords'] = []

        # follow back all followers?
        self.config['autofollow'] = True

        # ignore home timeline tweets which mention other accounts?
        self.config['ignore_timeline_mentions'] = False

        # ignore retweets in the home timeline
        self.config['ignore_timeline_retweets'] = True

        # max number of times to reply to someone within the moving window
        self.config['reply_threshold'] = 3

        # length of the moving window, in seconds
        self.config['recent_replies_window'] = 20*60

        # probability of replying to a matching timeline tweet
        self.config['timeline_reply_probability'] = float(os.environ.get('TIMELINE_REPLY_PROBABILITY') or '0.05')

        self.config['rotate_probability'] = float(os.environ.get('ROTATE_PROBABILITY') or '0.5')

        self.config['silent_mode'] = (int(os.environ.get('SILENT_MODE') or '1') != 0)

    def on_scheduled_tweet(self):
        pass

    def on_mention(self, tweet, prefix):
        if not self.check_reply_threshold(tweet, prefix):
            return

        self.reply_to_tweet(tweet, prefix)

    def on_timeline(self, tweet, prefix):
        if not self.check_reply_threshold(tweet, prefix):
            return

        if random.random() > self.config['timeline_reply_probability']:
            self.log("Failed dice roll. Not responding to {}".format(self._tweet_url(tweet)))
            return

        self.reply_to_tweet(tweet, prefix)

    def reply_to_tweet(self, tweet, prefix):
        self.log("Getting image blob for {}".format(self._tweet_url(tweet)))
        blob = None

        try:
            blob = self.get_image_blob(tweet)
        except NoImages:
            self.log("Couldn't find any images")
            return
        except ImageNotFound:
            self.log("Image URL returned a 404 - tweet was probably deleted")
            return

        self.log("Generating response")
        blob = self.generate_image(blob)

        prefix += ' '
        salutation = self.generate_salutation(tweet, 140-len(prefix))
        text = '{}{}'.format(prefix, salutation)

        if self._is_silent():
            self.log("Silent mode is on. Would've responded to {} with {}".format(self._tweet_url(tweet), repr(text)))
            return

        self.post_tweet(
            text,
            reply_to=tweet,
            media='not-actually-a-file.jpeg',
            file=BytesIO(blob),
        )
        self.update_reply_threshold(tweet, prefix)

    def generate_salutation(self, tweet, max_len=140):
        choices = [s.format(**tweet.__dict__) for s in SALUTATIONS]
        choices = [s for s in choices if len(s) <= max_len]

        if len(choices) == 0:
            return ''
        else:
            return random.choice(choices)

    def _is_silent(self):
        return self.config['silent_mode']

    def check_reply_threshold(self, tweet, prefix):
        self.trim_recent_replies()
        screen_names = self.get_screen_names(prefix)
        over_threshold = [sn for sn in screen_names if self.over_reply_threshold(sn)]

        if len(over_threshold) > 0:
            self.log("Over reply threshold for {}. Not responding to {}".format(", ".join(over_threshold), self._tweet_url(tweet)))
            return False

        return True

    def over_reply_threshold(self, screen_name):
        replies = [r for r in self.recent_replies() if screen_name in r['screen_names']]
        return len(replies) >= self.config['reply_threshold']

    def update_reply_threshold(self, tweet, prefix):
        screen_names = self.get_screen_names(prefix)

        self.recent_replies().append({
            'created_at': arrow.utcnow(),
            'screen_names': screen_names,
        })

        self.log("Updated recent_replies: len = {}".format(len(self.recent_replies())))

    def get_screen_names(self, prefix):
        return [sn.replace('@', '') for sn in prefix.split()]

    def trim_recent_replies(self):
        len_before = len(self.recent_replies())
        now = arrow.utcnow()
        self.state['recent_replies'] = [
            r for r in self.recent_replies()
            if (now - r['created_at']).seconds < self.config['recent_replies_window']
        ]
        self.log("Trimmed recent_replies: {} -> {}".format(len_before, len(self.recent_replies())))

    def recent_replies(self):
        if 'recent_replies' not in self.state:
            self.state['recent_replies'] = []
        return self.state['recent_replies']


    def generate_image(self, original):
        return wordpad(
            original,
            max_size=(1024, 1024),
            rotate=(random.random() <= self.config['rotate_probability']),
        )

    def get_image_blob(self, tweet):
        try:
            url = next(self.get_images(tweet))
        except StopIteration:
            raise NoImages()

        resp = requests.get(url)
        if resp.status_code == 404:
            raise ImageNotFound()
        else:
            resp.raise_for_status()

        return resp.content

    def get_images(self, tweet):
        for m in tweet.entities.get('media', []):
            if m.get('type') == 'photo':
                yield m['media_url']

        self.log("No images in {} - checking for a reply chain".format(self._tweet_url(tweet)))

        while tweet.in_reply_to_status_id:
            try:
                tweet = self.api.get_status(tweet.in_reply_to_status_id)
            except TweepError as e:
                self.log("Error climbing the reply chain: {}".format(e))
                return

            # don't glitch yourself mate
            if tweet.author.id == self.id:
                self.log("Found my own tweet ({}) - stopping".format(self._tweet_url(tweet)))
                return

            # don't reply more than once if there are multiple CCs.
            # probably overly cautious.
            if '@'+self.screen_name in tweet.text:
                self.log("Found a mention of myself ({}) - stopping".format(self._tweet_url(tweet)))
                return

            self.log("Climbing up to status {}".format(self._tweet_url(tweet)))
            for m in self.get_images(tweet):
                yield m


class NoImages(Exception):
    pass


class ImageNotFound(Exception):
    pass


if __name__ == '__main__':
    stderr = logging.StreamHandler()
    stderr.setLevel(logging.DEBUG)
    stderr.setFormatter(logging.Formatter(fmt='%(levelname)8s: %(message)s'))

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.addHandler(stderr)

    bot = WordPadBot()
    bot.run()
