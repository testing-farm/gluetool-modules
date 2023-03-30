# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

from gluetool.log import log_blob, log_dict

# Type annotations
from typing import TYPE_CHECKING, Dict, List, Optional, Union  # noqa
from gluetool.log import LoggingFunctionType # noqa


class Message(object):
    """
    An e-mail message. Bundles together message metadata and its content.

    The body of the message is split into three pieces, a header, a message and a footer.

    :param:
    :param str subject: subject of the e-mail.
    :param str header: `header` of the e-mail body.
    :param str footer: `footer` of the e-mail body.
    :param str body: the main part of the e-mail body.
    :param list(str) recipients: future recipients of the e-mail.
    :param list(str) cc: future CC recipients of the e-mail.
    :param str sender: e-mail sender address.
    :param str reply_to: if set, it is the value of ``Reply-To`` e-mail header.
    """

    def __init__(self,
                 subject: Optional[str] = None,
                 header: Optional[str] = None,
                 footer: Optional[str] = None,
                 body: Optional[str] = None,
                 recipients: Optional[List[str]] = None,
                 cc: Optional[List[str]] = None,
                 bcc: Optional[List[str]] = None,
                 sender: Optional[str] = None,
                 reply_to: Optional[str] = None,
                 xheaders: Optional[Dict[str, str]] = None
                ) -> None:  # noqa

        self.subject = subject or ''
        self.header = header or ''
        self.footer = footer or ''
        self.body = body or ''
        self.recipients = recipients or []
        self.cc = cc or []
        self.bcc = bcc or []
        self.sender = sender or ''
        self.reply_to = reply_to or ''
        self.xheaders = xheaders or {}

    def log(self, log_fn: LoggingFunctionType) -> None:
        """
        Log the message and its properties.
        """

        log_dict(log_fn, 'message metadata', {
            'sender': self.sender,
            'recipients': self.recipients,
            'cc': self.cc,
            'bcc': self.bcc,
            'reply-to': self.reply_to,
            'xheaders': self.xheaders
        })

        log_blob(log_fn, 'subject', self.subject)
        log_blob(log_fn, 'header', self.header)
        log_blob(log_fn, 'body', self.body)
        log_blob(log_fn, 'footer', self.footer)
