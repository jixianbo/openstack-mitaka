
# Copyright 2013 Red Hat, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import abc

from oslo_config import cfg
from oslo_utils import timeutils
import six
from six.moves import range as compat_range


from oslo_messaging import exceptions

base_opts = [
    cfg.IntOpt('rpc_conn_pool_size',
               default=30,
               deprecated_group='DEFAULT',
               help='Size of RPC connection pool.'),
]


def batch_poll_helper(func):
    """Decorator to poll messages in batch

    This decorator helps driver that polls message one by one,
    to returns a list of message.
    """
    def wrapper(in_self, timeout=None, prefetch_size=1):
        incomings = []
        driver_prefetch = in_self.prefetch_size
        if driver_prefetch > 0:
            prefetch_size = min(prefetch_size, driver_prefetch)
        watch = timeutils.StopWatch(duration=timeout)
        with watch:
            for __ in compat_range(prefetch_size):
                msg = func(in_self, timeout=watch.leftover(return_none=True))
                if msg is not None:
                    incomings.append(msg)
                else:
                    # timeout reached or listener stopped
                    break
        return incomings
    return wrapper


class TransportDriverError(exceptions.MessagingException):
    """Base class for transport driver specific exceptions."""


@six.add_metaclass(abc.ABCMeta)
class IncomingMessage(object):

    def __init__(self, ctxt, message):
        self.ctxt = ctxt
        self.message = message

    def acknowledge(self):
        "Acknowledge the message."

    @abc.abstractmethod
    def requeue(self):
        "Requeue the message."


@six.add_metaclass(abc.ABCMeta)
class RpcIncomingMessage(IncomingMessage):

    @abc.abstractmethod
    def reply(self, reply=None, failure=None, log_failure=True):
        "Send a reply or failure back to the client."


@six.add_metaclass(abc.ABCMeta)
class Listener(object):
    def __init__(self, prefetch_size=-1):
        self.prefetch_size = prefetch_size

    @abc.abstractmethod
    def poll(self, timeout=None, prefetch_size=1):
        """Blocking until 'prefetch_size' message is pending and return
        [IncomingMessage].
        Return None after timeout seconds if timeout is set and no message is
        ending or if the listener have been stopped.
        """

    def stop(self):
        """Stop listener.
        Stop the listener message polling
        """
        pass

    def cleanup(self):
        """Cleanup listener.
        Close connection (socket) used by listener if any.
        As this is listener specific method, overwrite it in to derived class
        if cleanup of listener required.
        """
        pass


@six.add_metaclass(abc.ABCMeta)
class BaseDriver(object):
    prefetch_size = 0

    def __init__(self, conf, url,
                 default_exchange=None, allowed_remote_exmods=None):
        self.conf = conf
        self._url = url
        self._default_exchange = default_exchange
        self._allowed_remote_exmods = allowed_remote_exmods or []

    def require_features(self, requeue=False):
        if requeue:
            raise NotImplementedError('Message requeueing not supported by '
                                      'this transport driver')

    @abc.abstractmethod
    def send(self, target, ctxt, message,
             wait_for_reply=None, timeout=None, envelope=False):
        """Send a message to the given target."""

    @abc.abstractmethod
    def send_notification(self, target, ctxt, message, version):
        """Send a notification message to the given target."""

    @abc.abstractmethod
    def listen(self, target):
        """Construct a Listener for the given target."""

    @abc.abstractmethod
    def listen_for_notifications(self, targets_and_priorities, pool):
        """Construct a notification Listener for the given list of
        tuple of (target, priority).
        """

    @abc.abstractmethod
    def cleanup(self):
        """Release all resources."""
