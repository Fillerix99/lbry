import logging
import json
from twisted.python import failure
from twisted.internet import error, defer
from twisted.internet.protocol import Protocol, ServerFactory
from lbrynet.core.utils import is_valid_blobhash
from lbrynet.core.Error import DownloadCanceledError
from lbrynet.reflector.common import REFLECTOR_V1, REFLECTOR_V2
from lbrynet.reflector.common import ReflectorRequestError, ReflectorClientVersionError


log = logging.getLogger(__name__)


class ReflectorServer(Protocol):
    def connectionMade(self):
        peer_info = self.transport.getPeer()
        log.debug('Connection made to %s', peer_info)
        self.peer = self.factory.peer_manager.get_peer(peer_info.host, peer_info.port)
        self.blob_manager = self.factory.blob_manager
        self.received_handshake = False
        self.received_descriptor = False
        self.protocol_version = REFLECTOR_V2
        self.peer_version = None
        self.receiving_blob = False
        self.incoming_blob = None
        self.blob_write = None
        self.blob_finished_d = None
        self.cancel_write = None
        self.request_buff = ""

    def connectionLost(self, reason=failure.Failure(error.ConnectionDone())):
        log.info("Reflector upload from %s finished" % self.peer.host)

    def dataReceived(self, data):
        if self.receiving_blob:
            self.blob_write(data)
        else:
            log.debug('Not yet recieving blob, data needs further processing')
            self.request_buff += data
            msg, extra_data = self._get_valid_response(self.request_buff)
            if msg is not None:
                self.request_buff = ''
                d = self.handle_request(msg)
                d.addErrback(self.handle_error)
                if self.receiving_blob and extra_data:
                    log.debug('Writing extra data to blob')
                    self.blob_write(extra_data)

    def _get_valid_response(self, response_msg):
        extra_data = None
        response = None
        curr_pos = 0
        while not self.receiving_blob:
            next_close_paren = response_msg.find('}', curr_pos)
            if next_close_paren != -1:
                curr_pos = next_close_paren + 1
                try:
                    response = json.loads(response_msg[:curr_pos])
                except ValueError:
                    if curr_pos > 200:
                        raise ValueError("Error decoding response: %s" % str(response_msg))
                    else:
                        pass
                else:
                    extra_data = response_msg[curr_pos:]
                    break
            else:
                break
        return response, extra_data

    def handle_request(self, request_dict):
        if self.received_handshake is False:
            return self.handle_handshake(request_dict)
        if self.received_descriptor is False and self.peer_version == REFLECTOR_V2:
            return self.handle_descriptor_request(request_dict)
        return self.handle_normal_request(request_dict)

    def handle_handshake(self, request_dict):
        log.debug('Handling handshake')
        if 'version' not in request_dict:
            raise ReflectorRequestError("Client should send version")
        self.peer_version = int(request_dict['version'])
        if self.peer_version not in [REFLECTOR_V1, REFLECTOR_V2]:
            raise ReflectorClientVersionError("Unknown version: %i" % self.peer_version)
        self.received_handshake = True
        d = defer.succeed({'version': self.peer_version})
        d.addCallback(self.send_response)
        return d

    def clean_up_failed_upload(self, err, blob):
        if err.check(DownloadCanceledError):
            err.trap(DownloadCanceledError)
            log.warning("Failed to receive %s", blob.blob_hash[:16])
            self.blob_manager.delete_blobs([blob.blob_hash])
        else:
            log.exception(err)
        return False

    def log_received_blob(self, blob, message):
        log.info(message, blob.blob_hash[:16])
        return True

    def determine_descriptor_needed(self, sd_blob):
        if sd_blob.is_validated():
            log.debug("Already have stream descriptor %s", str(sd_blob)[:16])
            d = self.determine_missing_blobs(sd_blob)
            d.addCallback(lambda needed_blobs:
                          {'send_sd_blob': False, 'needed_blobs': needed_blobs})
        else:
            log.info("Descriptor needed for new stream %s", str(sd_blob)[:16])
            log_msg = "Received sd blob %s"
            self.incoming_blob = sd_blob
            self.receiving_blob = True
            self.blob_finished_d, self.blob_write, self.cancel_write = sd_blob.open_for_writing(self.peer)  # pylint: disable=line-too-long
            self.blob_finished_d.addCallback(lambda _: self.blob_manager.blob_completed(sd_blob))
            self.blob_finished_d.addCallback(lambda _: self.close_blob())
            self.blob_finished_d.addCallback(lambda _: self.log_received_blob(sd_blob, log_msg))
            self.blob_finished_d.addErrback(self.clean_up_failed_upload, sd_blob)
            self.blob_finished_d.addCallback(lambda result: {'received_sd_blob': result})
            self.blob_finished_d.addCallback(self.send_response)
            d = defer.succeed({'send_sd_blob': True})
        return d

    def determine_missing_blobs(self, sd_blob):
        def _check_descriptor(sd_blob):
            for blob in sd_blob['blobs']:
                if 'blob_hash' in blob and 'length' in blob:
                    blob_hash, blob_len = blob['blob_hash'], blob['length']
                    _d = self.blob_manager.get_blob(blob_hash, True, blob_len)
                    _d.addCallback(lambda blob: blob_hash if not blob.is_validated() else None)
                    yield _d

        def _show_missing(missing, total):
            if missing:
                log.info("%i of %i blobs are needed", len(missing), total)
            else:
                log.debug("all blobs in stream are reflected")
            return missing

        with sd_blob.open_for_reading() as sd_file:
            raw_sd_blob_data = sd_file.read()

        decoded_sd_blob = json.loads(raw_sd_blob_data)
        dl = defer.DeferredList(list(_check_descriptor(decoded_sd_blob)))
        dl.addCallback(lambda to_skip: [blob[1] for blob in to_skip if blob[1]])
        dl.addCallback(_show_missing, len(decoded_sd_blob['blobs']))
        return dl

    def determine_blob_needed(self, blob):
        if blob.is_validated():
            return {'send_blob': False}
        else:
            log_msg = "Received blob %s"
            self.incoming_blob = blob
            self.receiving_blob = True
            self.blob_finished_d, self.blob_write, self.cancel_write = blob.open_for_writing(self.peer)  # pylint: disable=line-too-long
            self.blob_finished_d.addCallback(lambda _: self.blob_manager.blob_completed(blob))
            self.blob_finished_d.addCallback(lambda _: self.close_blob())
            self.blob_finished_d.addCallback(lambda _: self.log_received_blob(blob, log_msg))
            self.blob_finished_d.addErrback(self.clean_up_failed_upload, blob)
            self.blob_finished_d.addCallback(lambda result: {'received_blob': result})
            self.blob_finished_d.addCallback(self.send_response)
            return {'send_blob': True}

    def close_blob(self):
        self.blob_finished_d = None
        self.blob_write = None
        self.cancel_write = None
        self.incoming_blob = None
        self.receiving_blob = False

    def handle_descriptor_request(self, request_dict):
        if self.blob_write is None:
            if 'sd_blob_hash' not in request_dict or 'sd_blob_size' not in request_dict:
                err_msg = "Expected a sd blob hash and a sd blob size: %s"
                raise ReflectorRequestError(err_msg, request_dict)
            if not is_valid_blobhash(request_dict['sd_blob_hash']):
                err_msg = "Got a bad sd blob hash: {}".format(request_dict['sd_blob_hash'])
                raise ReflectorRequestError(err_msg)
            self.received_descriptor = True
            d = self.blob_manager.get_blob(
                request_dict['sd_blob_hash'],
                True,
                int(request_dict['sd_blob_size'])
            )
            d.addCallback(self.determine_descriptor_needed)
            d.addCallback(self.send_response)
        else:
            self.receiving_blob = True
            d = self.blob_finished_d
        return d

    def handle_normal_request(self, request_dict):
        if self.blob_write is None:
            #  we haven't opened a blob yet, meaning we must be waiting for the
            #  next message containing a blob hash and a length. this message
            #  should be it. if it's one we want, open the blob for writing, and
            #  return a nice response dict (in a Deferred) saying go ahead
            if not 'blob_hash' in request_dict or not 'blob_size' in request_dict:
                err_msg = "Expected a blob hash and a blob size: %s"
                raise ReflectorRequestError(err_msg, request_dict)
            if not is_valid_blobhash(request_dict['blob_hash']):
                err_msg = "Got a bad blob hash: {}".format(request_dict['blob_hash'])
                raise ReflectorRequestError(err_msg)
            log.debug('Received info for blob: %s', request_dict['blob_hash'][:16])
            d = self.blob_manager.get_blob(
                request_dict['blob_hash'],
                True,
                int(request_dict['blob_size'])
            )
            d.addCallback(self.determine_blob_needed)
            d.addCallback(self.send_response)
        else:
            #  we have a blob open already, so this message should have nothing
            #  important in it. to the deferred that fires when the blob is done,
            #  add a callback which returns a nice response dict saying to keep
            #  sending, and then return that deferred
            log.debug('blob is already open')
            self.receiving_blob = True
            d = self.blob_finished_d
        return d

    def send_response(self, response_dict):
        self.transport.write(json.dumps(response_dict))

    def handle_error(self, err):
        log.error(err.getTraceback())
        self.transport.loseConnection()


class ReflectorServerFactory(ServerFactory):
    protocol = ReflectorServer

    def __init__(self, peer_manager, blob_manager):
        self.peer_manager = peer_manager
        self.blob_manager = blob_manager

    def buildProtocol(self, addr):
        log.debug('Creating a protocol for %s', addr)
        return ServerFactory.buildProtocol(self, addr)
