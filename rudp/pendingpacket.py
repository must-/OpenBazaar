from pyee import EventEmitter
import logging
from tornado import ioloop


class PendingPacket(object):

    def __init__(self, packet, packet_sender):
        self.loop = ioloop.IOLoop.current()
        self.event_emitter = EventEmitter()

        self._packet_sender = packet_sender
        self._packet = packet
        self._interval_id = None
        self._sending = False
        self._sending_count = 0

        self.log = logging.getLogger(
            '%s' % self.__class__.__name__
        )

        self.log.info('Init PendingPacket')

    def send(self):

        self._sending = True

        self.log.debug('Sending Packet #%s: %s', self._packet.get_sequence_number(), self._sending)
        self._packet_sender.send(self._packet)

        # self._interval_id = rudp.helpers.set_interval(
        #     packet_send,
        #     rudp.constants.TIMEOUT
        # )

        # self.log.debug('Packet %s sent %d times', self._packet.get_sequence_number(), self._sending_count)

    def get_sequence_number(self):
        return self._packet.get_sequence_number()

    def acknowledge(self):
        self.log.debug('Pending Packet Acknowledged: %s', self._packet.get_sequence_number())
        self._sending = None

        if self._interval_id:
            self._interval_id.cancel()
            self._interval_id = None

        self.event_emitter.emit('acknowledge')
