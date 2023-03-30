# Copyright Contributors to the Testing Farm project.
# SPDX-License-Identifier: Apache-2.0

import gluetool

import mysql.connector

# Type annotations
from typing import Any  # noqa


class MySQL(gluetool.Module):
    """
    This module provides connection to a MySQL database via a database cursor.

    The cursor is compliant with Python Database API Specification 2.0, for its
    detailed documentation, see https://dev.mysql.com/doc/connector-python/en/.
    """

    name = 'mysql'
    description = 'Provides access to MySQL servers and databases.'

    options = {
        'host': {
            'help': 'Database server hostname (default: %(default)s).',
            'type': str,
            'default': 'localhost'
        },
        'port': {
            'help': 'Database server port number(default: %(default)s).',
            'type': int,
            'default': 3306
        },
        'user': {
            'help': 'Username (default: %(default)s).',
            'default': None
        },
        'password': {
            'help': 'Password (default: %(default)s).',
            'default': None
        },
        'dbname': {
            'help': 'Database name to connect to.'
        }
    }

    required_options = ('dbname',)
    shared_functions = ['db_cursor']

    def __init__(self, *args: Any, **kwargs: Any) -> None:

        super(MySQL, self).__init__(*args, **kwargs)

        self._connection = None

    @property
    def connection(self) -> Any:

        if self._connection is None:
            try:
                self._connection = mysql.connector.connect(user=self.option('user'), password=self.option('password'),
                                                           host=self.option('host'), port=self.option('port'),
                                                           database=self.option('dbname'))

            except mysql.connector.Error as exc:
                raise gluetool.GlueError('Failed to connect to the database: {}'.format(exc))

        return self._connection

    def db_cursor(self, **kwargs: Any) -> Any:
        """
        Return a database cursor.

        :raises gluetool.GlueError: When it is not possible to connect to the database.
        """

        return self.connection.cursor()

    def server_version(self) -> Any:

        cursor = self.db_cursor()

        cursor.execute('SELECT version()')
        row = cursor.fetchone()

        if row is None:
            raise gluetool.GlueError('Could not discover server version')

        return row[0]

    def execute(self) -> None:

        version = self.server_version()

        self.info("Connected to a MySQL '{}', version '{}'".format(self.option('host'), version))
