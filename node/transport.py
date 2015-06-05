from collections import defaultdict
import hashlib
import json
import logging
from pprint import pformat
import random
import sys
import traceback
import xmlrpclib
import time
import gnupg
import obelisk
import bitcoin
from sqlite3.dbapi2 import OperationalError, DatabaseError
from tornado import ioloop
from tornado.ioloop import PeriodicCallback

from node import connection, network_util, trust
from node.constants import MSG_PING_ID, MSG_PONG_ID, VERSION
from node.dht import DHT
from rudp.packet import Packet
from node.crypto_util import Cryptor
import string


class TransportLayer(object):
    """TransportLayer manages a list of peers."""

    def __init__(self, ob_ctx, guid, nickname=None, avatar_url=None):
        self.peers = {}
        self.callbacks = defaultdict(list)
        self.timeouts = []
        self.port = ob_ctx.server_port
        self.hostname = ob_ctx.server_ip
        self.nat_type = ob_ctx.nat_status['nat_type']
        self.guid = guid
        self.market_id = ob_ctx.market_id
        self.nickname = nickname
        self.avatar_url = avatar_url
        self.handler = None
        self.uri = network_util.get_peer_url(self.hostname, self.port)
        self.listener = None

        self.mediate_peers = []

        self.log = logging.getLogger(
            '[%s] %s' % (ob_ctx.market_id, self.__class__.__name__)
        )

    def add_callbacks(self, callbacks):
        for section, callback in callbacks:
            self.callbacks[section] = []
            self.add_callback(section, callback)

    def set_websocket_handler(self, handler):
        self.handler = handler

    def add_callback(self, section, callback):
        if callback not in self.callbacks[section]:
            self.callbacks[section].append(callback)

    def trigger_callbacks(self, section, *data):
        """Run all callbacks in specified section."""
        for callback in self.callbacks[section]:
            if callback['validator_cb'](*data):
                callback['cb'](*data)

        # Run all callbacks registered under the 'all' section. Don't duplicate
        # calls if the specified section was 'all'.
        if not section == 'all':
            for callback in self.callbacks['all']:
                if callback['validator_cb'](*data):
                    callback['cb'](*data)


class CryptoTransportLayer(TransportLayer):

    def __init__(self, ob_ctx, db_connection):

        self.ob_ctx = ob_ctx
        self.loop = ioloop.IOLoop.current()
        self.log = logging.getLogger(
            '[%s] %s' % (ob_ctx.market_id, self.__class__.__name__)
        )
        requests_log = logging.getLogger("requests")
        requests_log.setLevel(logging.WARNING)

        self.db_connection = db_connection

        self.bitmessage_api = None
        if (ob_ctx.bm_user, ob_ctx.bm_pass, ob_ctx.bm_port) != (None, None, -1):
            if not self._connect_to_bitmessage():
                self.log.info('Bitmessage not installed or started')

        self.market_id = ob_ctx.market_id
        self.nick_mapping = {}
        self.uri = network_util.get_peer_url(ob_ctx.server_ip, ob_ctx.server_port)
        self.hostname = ob_ctx.server_ip
        self.nickname = ""
        self.avatar_url = ""
        self.dev_mode = ob_ctx.dev_mode
        self.seed_mode = ob_ctx.seed_mode
        self.mediation_mode = {}
        self.guid = ""
        self.sin = ""

        self._connections = {}
        self._punches = {}

        self.all_messages = (
            'hello',
            'hello_response',
            'goodbye',
            'findNode',
            'findNodeResponse',
            'store',
            'mediate',
            'register',
            'punch',
            MSG_PING_ID,
            MSG_PONG_ID,
            'get_nat_type',
            'nat_type',
            'relay_msg'
        )

        self._setup_settings()
        ob_ctx.market_id = self.market_id
        self.dht = DHT(self, self.market_id, self.settings, self.db_connection)
        TransportLayer.__init__(self, ob_ctx, self.guid, self.nickname, self.avatar_url)
        self.start_listener()

        self.ip_checker_caller = None
        if ob_ctx.enable_ip_checker and not ob_ctx.seed_mode and not ob_ctx.dev_mode:
            self.start_ip_address_checker()

        # self.loop.call_later(5, self.truncate_dead_peers)

    def truncate_dead_peers(self):
        for peer in self.dht.active_peers:
            print 'last reached: ', peer.last_reached
            if peer.last_reached != 0 and time.time() - peer.last_reached <= 15:
                if peer.guid:
                    self.dht.remove_peer(peer.guid)

    def relay_message(self, data, guid):
        for peer in self.dht.active_peers:
            if peer.hostname == '205.186.156.31' or peer.hostname == 'seed2.openbazaar.org':
                peer.send_raw(json.dumps({
                    'type': 'relay_msg',
                    'data': data.encode('hex'),
                    'guid': guid,
                    'senderGUID': self.guid,
                    'v': VERSION
                }), relay=True)

    def start_mediation(self, guid):

        if not self.mediation_mode[guid]:
            self.mediation_mode[guid] = True

            self.log.debug('Starting mediation')

            for peer in self.dht.active_peers:
                if peer.hostname == '127.0.0.1' or peer.hostname == '205.186.156.31' \
                        or peer.hostname == 'seed2.openbazaar.org':
                    peer.send_raw(json.dumps({
                        'type': 'mediate',
                        'guid': self.guid,
                        'guid2': guid,
                        'senderGUID': self.guid,
                        'v': VERSION
                    }))

                    # def heartbeat():
                    #     peer.sock.sendto('MSG_HEARTBEAT_ID', (peer.hostname, peer.port))

                    # Heartbeat to relay server
                    # PeriodicCallback(heartbeat, 5000, self.loop).start()

    def get_nat_type(self, guid):
        self.log.debug('Requesting nat type for user: %s', guid)
        for peer in self.dht.active_peers:
            if peer.hostname in ('127.0.0.1', '205.186.156.31', 'seed2.openbazaar.org'):
                peer.send({
                    'type': 'get_nat_type',
                    'peer_guid': guid,
                    'v': VERSION
                })

    def update_avatar(self, guid, avatar_url):
        peer = self.dht.routing_table.get_contact(guid)
        if peer:
            peer.avatar_url = avatar_url
        else:
            self.log.error('Cannot find peer to update avatar')

    def start_listener(self):
        self.add_callbacks([
            (
                msg,
                {
                    'cb': getattr(self, 'on_%s' % msg),
                    'validator_cb': getattr(self, 'validate_on_%s' % msg)
                }
            )
            for msg in self.all_messages
        ])

        self.listener = connection.CryptoPeerListener(
            self.hostname, self.port, self.pubkey, self.secret,
            self.guid,
            self._on_message
        )

        # pylint: disable=unused-variable
        @self.listener.event_emitter.on('on_pong_message')
        def on_pong_message(msg):
            data, addr = msg[0], msg[1]
            for active_peer in self.dht.active_peers:
                if active_peer.hostname == addr[0] and active_peer.port == addr[1]:
                    active_peer.reachable = True
                    active_peer.last_reached = time.time()

        # pylint: disable=unused-variable
        @self.listener.event_emitter.on('on_relay_pong_message')
        def on_relay_pong_message(msg):
            data, addr = msg[0], msg[1]
            data = data.split(' ')
            for active_peer in self.dht.active_peers:
                if active_peer.guid == data[1]:
                    active_peer.reachable = True
                    active_peer.last_reached = time.time()

        # pylint: disable=unused-variable
        @self.listener.event_emitter.on('on_send_relay_ping')
        def on_send_relay_ping(msg):
            data, addr = msg[0], msg[1]
            data = data.split(' ')
            peer = self.dht.routing_table.get_contact(data[1])
            if peer:
                peer.send_to_sock('relay_ping %s' % peer.guid)
            else:
                self.log.info('Could not find peer to send relay_ping to.')

        # pylint: disable=unused-variable
        @self.listener.event_emitter.on('on_send_relay_pong')
        def on_send_relay_pong(msg):
            data, addr = msg[0], msg[1]
            data = data.split(' ')
            peer = self.dht.routing_table.get_contact(data[2])
            if peer:
                peer.send_to_sock('relay_pong %s' % data[1])
            else:
                self.log.info('Could not find peer to send relay_pong to.')

        # pylint: disable=unused-variable
        @self.listener.event_emitter.on('on_relayto')
        def on_relayto(data):

            data = data.split(' ', 4)
            self.log.debug('RelayTo Data: %s', data)
            if len(data) <= 5:
                peer = self.dht.routing_table.get_contact(data[1])

                if peer:
                    peer.send_to_sock('relay %s' % data[4])
                else:
                    self.log.debug('Relaying to %s:%s', data[2], data[3])
                    self.listener.socket.sendto('relay %s' % data[4], (data[2], int(data[3])))

        # pylint: disable=unused-variable
        @self.listener.event_emitter.on('on_message')
        def on_message(msg):

            data, addr = msg[0], msg[1]
            self.log.debug('Got Packet: %s from %s', data, addr)
            relayed_message = False

            # Punch message
            if data[:5] == 'punch':
                data = data.split(' ')
                guid = data[1]
                peer = self.dht.routing_table.get_contact(guid)
                if peer:
                    peer.reachable = True
                    peer.relaying = False
                    peer._rudp_connection._sender._packet_sender.reachable = True
                    peer._rudp_connection._sender._packet_sender.relaying = False
                else:
                    self.log.debug('Do not know about this peer yet.')
                return

            try:

                # Relayed message
                if data[:6] == 'relay ':
                    msg_parts = data.split(' ', 1)
                    data = msg_parts[1]
                    data_body = json.loads(data)
                    hostname = data_body.get('hostname')
                    port = data_body.get('port')
                    relayed_message = True
                else:
                    data_body = json.loads(data)
                    port = addr[1]
                    hostname = addr[0]

                # Peer metadata
                guid = data_body.get('guid')
                pubkey = data_body.get('pubkey')
                nickname = data_body.get('nick')
                nat_type = data_body.get('nat_type')

                inbound_peer = self.dht.add_peer(hostname, port, pubkey, guid, nickname, nat_type)

                if inbound_peer:
                    inbound_peer.reachable = True

                    if relayed_message:
                        inbound_peer._rudp_connection._sender._packet_sender.relaying = True

                    packet = Packet(data, packet_buffer=True)

                    if packet._finish:

                        inbound_peer.reset()
                        return
                        # del self._connections[address_key]
                    else:

                        inbound_peer._rudp_connection.receive(packet)

                else:
                    self.log.debug('Did not find a peer')
            except Exception as exc:
                self.log.error('Could not deserialize message: %s', exc)

        self.listener.set_ok_msg({
            'type': 'ok',
            'senderGUID': self.guid,
            'pubkey': self.pubkey,
            'senderNick': self.nickname,
            'v': VERSION
        })
        self.listener.listen()

    def start_ip_address_checker(self):
        '''Checks for possible public IP change'''
        if self.ob_ctx.enable_ip_checker:
            self.ip_checker_caller = PeriodicCallback(self._ip_updater_periodic_callback, 5000, self.loop)
            self.ip_checker_caller.start()
            self.log.info("IP_CHECKER_ENABLED: Periodic IP Address Checker started.")

    def _ip_updater_periodic_callback(self):
        if self.ob_ctx.enable_ip_checker:
            new_ip = network_util.get_my_ip()

            if not new_ip or new_ip == self.hostname:
                return

            self.ob_ctx.server_ip = new_ip
            self.hostname = new_ip

            if self.listener is not None:
                self.listener.set_ip_address(new_ip)

            self.dht.iterative_find(self.guid, [], 'findNode')

    def save_peer_to_db(self, peer_tuple):
        hostname = peer_tuple[0]
        port = peer_tuple[1]
        pubkey = peer_tuple[2]
        guid = peer_tuple[3]
        nickname = peer_tuple[4]

        self.db_connection.deleteEntries("peers", {"hostname": hostname, "guid": guid}, "OR")
        if guid is not None:
            self.db_connection.insertEntry("peers", {
                "hostname": hostname,
                "port": port,
                "pubkey": pubkey,
                "guid": guid,
                "nickname": nickname,
                "market_id": self.market_id
            })

    def _connect_to_bitmessage(self):
        # Get bitmessage going
        # First, try to find a local instance
        result = False
        bm_user = self.ob_ctx.bm_user
        bm_pass = self.ob_ctx.bm_pass
        bm_port = self.ob_ctx.bm_port
        try:
            self.log.info(
                '[_connect_to_bitmessage] Connecting to Bitmessage on port %s',
                bm_port
            )
            self.bitmessage_api = xmlrpclib.ServerProxy(
                "http://{}:{}@localhost:{}/".format(bm_user, bm_pass, bm_port),
                verbose=0
            )
            result = self.bitmessage_api.add(2, 3)
            self.log.info(
                "[_connect_to_bitmessage] Bitmessage API is live: %s",
                result
            )
        # If we failed, fall back to starting our own
        except Exception as exc:
            self.log.info("Failed to connect to bitmessage instance: %s", exc)
            self.bitmessage_api = None
        return result

    def validate_on_goodbye(self, msg):
        self.log.debug('Validating goodbye message.')
        return True

    def on_goodbye(self, msg):
        self.log.info('Received Goodbye: %s', json.dumps(msg, ensure_ascii=False))
        self.dht.remove_peer(msg.get('senderGUID'))

    def validate_on_mediate(self, msg):
        """

        :param msg:
        :return:
        """
        self.log.debug('Validating mediate message.')
        return self.ob_ctx.seed_mode

    def on_mediate(self, msg):
        self.log.info('Received Mediate Message: %s', json.dumps(msg, ensure_ascii=False))

        if msg['guid2'] == self.guid:
            return

        def send_punches():

            peer1, peer2 = None, None

            # Send both peers a message to message each other
            for active_peer in self.dht.active_peers:
                if active_peer.guid == msg['senderGUID']:
                    self.log.debug('Found guid')
                    peer1 = active_peer
                if active_peer.guid == msg['guid2']:
                    self.log.debug('Found guid2')
                    peer2 = active_peer
                if peer1 and peer2:
                    continue

            if peer1 and peer2:
                self.log.debug('Sending Punches')

                peer1.send_raw(json.dumps({
                    'type': 'punch',
                    'guid': peer2.guid,
                    'hostname': peer2.hostname,
                    'port': peer2.port,
                    'pubkey': peer2.pub,
                    'senderGUID': peer2.guid,
                    'senderNick': peer2.nickname,
                    'v': VERSION
                }))

                peer2.send_raw(json.dumps({
                    'type': 'punch',
                    'guid': peer1.guid,
                    'hostname': peer1.hostname,
                    'port': peer1.port,
                    'pubkey': peer1.pub,
                    'senderGUID': peer1.guid,
                    'senderNick': peer1.nickname,
                    'v': VERSION
                }))
                return
            else:
                self.loop.call_later(5, send_punches)
        send_punches()

    def validate_on_relay_msg(self, msg):
        self.log.debug('Validating relay msg')
        return True

    def on_relay_msg(self, msg):
        self.log.debug('Relaying message to peer')
        peer = self.dht.routing_table.get_contact(msg['guid'])
        if peer:
            peer.send_raw(json.dumps({
                'type': 'relayed_msg',
                'data': msg['data'],
                'v': VERSION
            }), relay=True)
        else:
            self.log.debug('Could not find peer to relay to.')

    def validate_on_relayed_msg(self, msg):
        self.log.debug('Validating relayed msg')
        return True

    def on_relayed_msg(self, msg):
        self.log.debug('Received relayed message to peer')
        peer = self.dht.routing_table.get_contact(msg['guid'])
        if peer:
            peer.send_raw(json.dumps({
                'type': 'relayed_msg',
                'data': msg['data'],
                'v': VERSION
            }), relay=True)
        else:
            self.log.debug('Could not find peer to relay to.')

    def validate_on_punch(self, msg):
        self.log.debug('Validating on punch')
        return True

    def on_punch(self, msg):
        self.log.debug('Got a punch request')

        peer = self.dht.add_peer(
            msg['hostname'], msg['port'], msg['pubkey'], msg['guid'], msg['senderNick']
        )

        if not peer.punching:
            def send(count):
                # Send raw socket punch
                peer.sock.sendto('punch %s' % self.guid, (peer.hostname, peer.port))
                self.log.debug('Sending punch to %s:%d', peer.hostname, peer.port)
                self.log.debug("UDP punching package %d sent", count)
                if peer.punching:
                    self.loop.call_later(0.5, send, count + 1)
                if count >= 25:
                    if not peer.reachable:
                        self.log.debug('Falling back to relaying.')
                        peer.relaying = True
                        peer._rudp_connection._sender._packet_sender.relaying = True
                        # self.init_packetsender()
                        # self.setup_emitters()
                        peer.reachable = True
                        peer.punching = False
                    else:
                        peer.punching = False
                        self.mediation_mode[peer.guid] = False

            peer.punching = True
            send(0)

    def validate_on_register(self, msg):
        self.log.debug('Validating register message.')
        return self.ob_ctx.seed_mode

    def on_register(self, msg):
        self.log.info('Received register: %s', json.dumps(msg, ensure_ascii=False))

        # Add entry to mediator table
        self.mediate_peers = [x for x in self.mediate_peers if not x.get('guid') == msg['senderGUID']]
        self.mediate_peers.append({
            'guid': msg['senderGUID'],
            'hostname': msg['hostname'],
            'port': msg['port']
        })

    def validate_on_get_nat_type(self, msg):
        self.log.debug('Validating %s', msg['type'])
        return True

    def on_get_nat_type(self, msg):
        self.log.debug('Finding nat type for user: %s', msg['peer_guid'])

        if msg['peer_guid'] == self.guid:
            self.log.debug('get_nat_type requested for yourself')
            return

        peer = self.dht.routing_table.get_contact(msg['peer_guid'])
        requester = self.dht.routing_table.get_contact(msg['senderGUID'])

        if peer:
            nat_type_msg = {
                'type': 'nat_type',
                'senderGUID': self.guid,
                'hostname': self.hostname,
                'port': self.port,
                'senderNICK': self.nickname,
                'nat_type': peer.nat_type,
                'peer_guid': peer.guid,
                'v': VERSION
            }
            requester.send_raw(json.dumps(nat_type_msg))
        else:
            self.log.error('No peer found for this GUID.')

    def validate_on_nat_type(self, msg):
        self.log.debug('Validating %s', msg['type'])
        return True

    def on_nat_type(self, msg):
        self.log.debug('Received nat type for user: %s', msg['peer_guid'])

        for peer in self.dht.active_peers:
            if peer.guid == msg['peer_guid']:
                peer.nat_type = msg['nat_type']
                if peer.nat_type == 'Symmetric NAT':
                    peer.relaying = True
                    peer._rudp_connection._sender._packet_sender.relaying = True
                    # self.init_packetsender()
                    # self.setup_emitters()
                    peer.reachable = True
                self.log.debug(peer)
                return

        self.log.error('No peer found for this GUID.')

    def validate_on_ping(self, *data):
        self.log.debug('Validating on ping message.')
        return True

    def on_ping(self, msg):
        self.log.debug('Got a ping message from: %s:%d', self.hostname, self.port)

        peer = self.dht.routing_table.get_contact(msg['senderGUID'])

        if peer:
            pong_msg = {
                'type': MSG_PONG_ID,
                'senderGUID': self.guid,
                'hostname': self.hostname,
                'port': self.port,
                'senderNICK': self.nickname,
                'avatar_url': self.avatar_url,
                'nat_type': self.nat_type,
                'v': VERSION
            }
            peer.send_raw(json.dumps(pong_msg))
        else:
            self.log.error('No peer found.')

    def validate_on_pong(self, *data):
        self.log.debug('Validating on pong message.')
        return True

    def on_pong(self, msg):
        self.log.debug('Got a pong message from: %s', msg['senderGUID'])
        peer = self.dht.routing_table.get_contact(msg['senderGUID'])
        peer.nat_type = msg['nat_type']
        peer.waiting = False
        peer.reachable = True
        self.log.debug('Updated peer object: %s', peer)

    def validate_on_hello(self, msg):
        self.log.debug('Validating hello message.')
        return True

    def on_hello(self, msg):

        self.log.info('Received Hello: %s', json.dumps(msg, ensure_ascii=False))

        peer = self.dht.add_peer(
            msg['hostname'],
            msg['port'],
            msg['pubkey'],
            msg['senderGUID'],
            msg['senderNick'],
            msg['nat_type'],
            msg['avatar_url']
        )

        peer.nat_type = msg['nat_type']

        # if peer:
        #     peer.send_raw(
        #         json.dumps({
        #             'type': 'hello_response',
        #             'pubkey': self.pubkey,
        #             'senderGUID': self.guid,
        #             'hostname': self.hostname,
        #             'nat_type': self.nat_type,
        #             'port': self.port,
        #             'senderNick': self.nickname,
        #             'v': VERSION
        #         })
        #     )

    def validate_on_hello_response(self, msg):
        self.log.debug('Validating hello response message.')
        return True

    def on_hello_response(self, msg):
        self.log.info('Received Hello Response: %s', json.dumps(msg, ensure_ascii=False))
        peer = self.dht.routing_table.get_contact(msg['senderGUID'])
        peer.nat_type = msg['nat_type']

    def validate_on_store(self, msg):
        self.log.debugv('Validating store value message.')
        return True

    def on_store(self, msg):
        self.dht._on_store_value(msg)

    def validate_on_findNode(self, msg): # pylint: disable=invalid-name
        self.log.debugv('Validating find node message.')
        return True

    def on_findNode(self, msg): # pylint: disable=invalid-name
        self.dht.on_find_node(msg)

    def validate_on_findNodeResponse(self, msg): # pylint: disable=invalid-name
        self.log.debugv('Validating find node response message.')
        return True

    def on_findNodeResponse(self, msg): # pylint: disable=invalid-name
        self.dht.on_find_node_response(msg)

    def _setup_settings(self):
        try:
            self.settings = self.db_connection.select_entries("settings", {"market_id": self.market_id})
        except (OperationalError, DatabaseError) as err:
            print err
            raise SystemExit("database file %s corrupt or empty - cannot continue" % self.db_connection.db_path)

        if len(self.settings) == 0:
            self.settings = {"market_id": self.market_id, "welcome": "enable"}
            self.db_connection.insert_entry("settings", self.settings)
        else:
            self.settings = self.settings[0]

        # Generate PGP key during initial setup or if previous PGP gen failed
        if not self.settings.get('PGPPubKey'):
            try:
                self.log.info('Generating PGP keypair. This may take several minutes...')
                print 'Generating PGP keypair. This may take several minutes...'
                gpg = gnupg.GPG()
                input_data = gpg.gen_key_input(key_type="RSA",
                                               key_length=4096,
                                               name_email='pgp@openbazaar.org',
                                               name_comment="Autogenerated by Open Bazaar",
                                               passphrase="P@ssw0rd")
                assert input_data is not None
                key = gpg.gen_key(input_data)
                assert key is not None

                pubkey_text = gpg.export_keys(key.fingerprint)
                newsettings = {"PGPPubKey": pubkey_text, "PGPPubkeyFingerprint": key.fingerprint}
                self.db_connection.update_entries("settings", newsettings, {"market_id": self.market_id})
                self.settings.update(newsettings)

                self.log.info('PGP keypair generated.')
            except Exception as exc:
                sys.exit("Encountered a problem with GPG: %s" % exc)

        if not self.settings.get('pubkey'):
            # Generate Bitcoin keypair
            self._generate_new_keypair()

        if not self.settings.get('nickname'):
            newsettings = {'nickname': 'User %s' % ''.join(random.choice(string.lowercase) for i in range(5))}
            self.db_connection.update_entries('settings', newsettings, {"market_id": self.market_id})
            self.settings.update(newsettings)

        self.nickname = self.settings.get('nickname', '')
        self.avatar_url = self.settings.get('avatar_url', '')
        self.namecoin_id = self.settings.get('namecoin_id', '')

        self.secret = self.settings.get('secret', '')
        self.pubkey = self.settings.get('pubkey', '')
        self.cryptor = Cryptor(pubkey_hex=self.pubkey, privkey_hex=self.secret)

        if not self.guid:
            self._setup_guid()

        self.guid = self.settings.get('guid', '')
        self.sin = self.settings.get('sin', '')
        self.bitmessage = self.settings.get('bitmessage', '')

        if not self.settings.get('bitmessage'):
            # Generate Bitmessage address
            if self.bitmessage_api is not None:
                self._generate_new_bitmessage_addr()

        # In case user wants to override with command line passed bitmessage values
        if self.ob_ctx.bm_user is not None and \
           self.ob_ctx.bm_pass is not None and \
           self.ob_ctx.bm_port is not None:
            self._connect_to_bitmessage()

    def _setup_guid(self):
        # GUIDs should be the pubkey signed with privkey then
        # hashed with SHA256 and subsequently RIPEMD160
        # This can provide protection against Spartacus
        # http://xlattice.sourceforge.net/components/protocol/kademlia/specs.html
        signed_pubkey = self.cryptor.sign(self.pubkey)
        sha_hash = hashlib.sha256()
        sha_hash.update(signed_pubkey)
        ripe_hash = hashlib.new('ripemd160')
        ripe_hash.update(sha_hash.digest())

        self.guid = ripe_hash.hexdigest()

        # Generate SIN
        self.sin = obelisk.EncodeBase58Check('\x0F\x02%s' % ripe_hash.digest())

        new_settings = {
            "guid": self.guid,
            "sin": self.sin
        }

        self.db_connection.update_entries("settings", new_settings, {"market_id": self.market_id})
        self.settings.update(new_settings)

    def _generate_new_keypair(self):

        seed = str(random.randrange(2 ** 256))

        # Deprecated (pre-BIP32)
        # self.secret = hashlib.sha256(secret).hexdigest()
        # self.pubkey = privkey_to_pubkey(self.secret)
        # self.log.debug('Keys %s %s', self.secret, self.pubkey)

        # Move to BIP32 keys m/0/0/0
        wallet = bitcoin.bip32_ckd(bitcoin.bip32_master_key(seed), 0)
        wallet_chain = bitcoin.bip32_ckd(wallet, 0)
        bip32_identity_priv = bitcoin.bip32_ckd(wallet_chain, 0)
        identity_priv = bitcoin.bip32_extract_key(bip32_identity_priv)
        bip32_identity_pub = bitcoin.bip32_privtopub(bip32_identity_priv)
        identity_pub = bitcoin.encode_pubkey(bitcoin.bip32_extract_key(bip32_identity_pub), 'hex')

        self.pubkey = identity_pub
        self.secret = identity_priv

        new_settings = {
            "secret": self.secret,
            "pubkey": self.pubkey,
            "bip32_seed": seed
        }
        self.db_connection.update_entries("settings", new_settings, {"market_id": self.market_id})
        self.settings.update(new_settings)

    def _generate_new_bitmessage_addr(self):
        # Use the guid generated previously as the key
        self.bitmessage = self.bitmessage_api.createRandomAddress(
            self.guid.encode('base64'),
            False,
            1.05,
            1.1111
        )
        newsettings = {"bitmessage": self.bitmessage}
        self.db_connection.update_entries("settings", newsettings, {"market_id": self.market_id})
        self.settings.update(newsettings)

    def join_network(self, seeds=None, callback=None):
        if not seeds:
            seeds = self.ob_ctx.seeds if not self.ob_ctx.seed_mode else []

        self.log.info('Joining network')

        # Connect up through seed servers
        for idx, seed in enumerate(seeds):
            seeds[idx] = (seed[0], int(seed[1]))

        # Connect to persisted peers
        db_peers = self.get_past_peers()

        known_peers = list(set(seeds).union(db_peers))

        for known_peer in known_peers:

            hostname, port = known_peer[0], known_peer[1]

            peer_obj = self.get_crypto_peer(None, hostname, port)

            self.dht.active_peers.append(peer_obj)

            peer_obj.seed = True
            peer_obj.reachable = True  # Seeds should be reachable always

        self.loop.call_later(30, self.search_for_my_node)

        if callback is not None:
            callback('Joined')

    def get_past_peers(self):
        result = self.db_connection.select_entries("peers", {"market_id": self.market_id})
        return [(peer['hostname'], peer['port']) for peer in result]

    def search_for_my_node(self):
        self.log.info('Searching for myself')
        self.dht.iterative_find('0000000000000000000000000000000000000000', [], 'findNode')

    def get_crypto_peer(self, guid=None, hostname=None, port=None, pubkey=None,
                        nickname=None, nat_type=None, avatar_url=None):
        if guid == self.guid:
            self.log.error('Cannot get CryptoPeerConnection for your own node')
            return

        if not guid:
            guid = 'seed%d' % len(self.peers)

        if guid not in self.peers:
            self.peers[guid] = connection.CryptoPeerConnection(
                self,
                hostname,
                port,
                pubkey,
                guid=guid,
                nickname=nickname,
                peer_socket=self.listener.socket,
                nat_type=nat_type,
                avatar_url=avatar_url
            )
        else:
            # FIXME this is wrong to do here, but it keeps this as close as
            # possible to the original pre-connection-reuse behavior
            if hostname:
                self.peers[guid].hostname = hostname
            if port:
                self.peers[guid].port = port
            if pubkey:
                self.peers[guid].pub = pubkey
            if nickname:
                self.peers[guid].nickname = nickname
            if nat_type:
                self.peers[guid].nat_type = nat_type
            if avatar_url:
                self.peers[guid].avatar_url = avatar_url

        return self.peers[guid]

    def send(self, data, send_to=None, callback=None):

        # Directed message
        if send_to is not None:

            peer = self.dht.routing_table.get_contact(send_to)
            if peer is None:
                for active_peer in self.dht.active_peers:
                    if active_peer.guid == send_to:
                        peer = active_peer
                        break

            if peer:
                msg_type = data.get('type', 'unknown')
                nickname = peer.nickname
                hostname = peer.hostname

                self.log.info('Sending message type "%s" to "%s" %s %s',
                              msg_type, nickname, hostname, send_to)
                self.log.datadump('Raw message: %s', data)

                try:
                    peer.send(data, callback=callback)
                except Exception as exc:
                    self.log.error('Failed to send message directly to peer %s', exc)

            else:
                self.log.warning("Couldn't find peer %s to send message type %s",
                                 send_to, data.get('type'))

        else:
            # FindKey and then send

            for peer in self.dht.active_peers:
                try:
                    routing_peer = self.dht.routing_table.get_contact(peer.guid)

                    if routing_peer is None:
                        self.dht.routing_table.add_contact(peer)
                        routing_peer = peer

                    data['senderGUID'] = self.guid
                    data['pubkey'] = self.pubkey

                    def log_callback(msg):
                        self.log.debug('Message Back: \n%s', pformat(msg))

                    routing_peer.send(data, log_callback)

                except Exception:
                    self.log.info("Error sending over peer!")
                    traceback.print_exc()

    def _on_message(self, msg):

        # here goes the application callbacks
        # we get a "clean" msg which is a dict holding whatever
        hostname = msg.get('hostname')
        guid = msg.get('senderGUID')
        nickname = msg.get('senderNick', '')
        nickname = nickname[:120] if nickname else ''
        msg_type = msg.get('type')
        namecoin = msg.get('senderNamecoin', '')

        # Checking for malformed URIs
        # if not network_util.is_valid_uri(uri):
        #     self.log.error('Malformed URI: %s', uri)
        #     return

        # Validate the claimed namecoin in DNSChain
        if not trust.is_valid_namecoin(namecoin, guid):
            msg['senderNamecoin'] = ''

        self.log.info('Received message type "%s" from "%s" %s %s',
                      msg_type, nickname, hostname, guid)
        self.log.datadump('Raw message: %s', json.dumps(msg, ensure_ascii=False))
        #self.dht.add_peer(uri, pubkey, guid, nickname)
        self.trigger_callbacks(msg['type'], msg)

    def store(self, *args, **kwargs):
        """
        Store or republish data.

        Refer to the dht module (iterative_store()) for further details.
        """
        self.dht.iterative_store(*args, **kwargs)

    def shutdown(self):
        print "CryptoTransportLayer.shutdown()!"
        print "Notice: explicit DHT Shutdown not implemented."

        try:
            if self.bitmessage_api is not None:
                self.bitmessage_api.close()
        except Exception as exc:
            # It might not even be open; we can't do much more on our
            # way out if exception is thrown here.
            self.log.error(
                "Could not shutdown bitmessage_api's ServerProxy: %s", exc.message
            )
