import hashlib
import struct
import time

import msgpack
from electrumx.lib.hash import hash_to_str

from electrumx.server.block_processor import BlockProcessor
from lbryschema.proto.claim_pb2 import Claim
from lbryschema.uri import parse_lbry_uri
from lbryschema.decode import smart_decode

from lbryumx.model import NameClaim, ClaimInfo, ClaimUpdate, ClaimSupport


class LBRYBlockProcessor(BlockProcessor):

    def __init__(self, *args, **kwargs):
        self.claim_cache = {}
        self.claims_for_name_cache = {}
        self.claims_signed_by_cert_cache = {}
        self.outpoint_to_claim_id_cache = {}
        self.claims_db = self.names_db = self.signatures_db = self.outpoint_to_claim_id_db = self.claim_undo_db = None
        super().__init__(*args, **kwargs)

        # stores deletes not yet flushed to disk
        self.pending_abandons = {}
        self.should_validate_signatures = self.env.boolean('VALIDATE_CLAIM_SIGNATURES', False)
        self.log_info("LbryumX Block Processor - Validating signatures: {}".format(self.should_validate_signatures))

    def open_dbs(self):
        super().open_dbs()
        def log_reason(message, is_for_sync):
            reason = 'sync' if is_for_sync else 'serving'
            self.logger.info('{} for {}'.format(message, reason))

        for for_sync in [False, True]:
            if self.claims_db:
                if self.claims_db.for_sync == for_sync:
                    return
                log_reason('closing claim DBs to re-open', for_sync)
                self.claims_db.close()
                self.names_db.close()
                self.signatures_db.close()
                self.outpoint_to_claim_id_db.close()
                self.claim_undo_db.close()
            self.claims_db = self.db_class('claims', for_sync)
            self.names_db = self.db_class('names', for_sync)
            self.signatures_db = self.db_class('signatures', for_sync)
            self.outpoint_to_claim_id_db = self.db_class('outpoint_claim_id', for_sync)
            self.claim_undo_db = self.db_class('claim_undo', for_sync)
            log_reason('opened claim DBs', self.claims_db.for_sync)

    def flush(self, flush_utxos=False):
        # flush claims together with utxos as they are parsed together
        self.batched_flush_claims()
        return super().flush(flush_utxos=flush_utxos)

    def batched_flush_claims(self):
        with self.claims_db.write_batch() as claims_batch:
            with self.names_db.write_batch() as names_batch:
                with self.signatures_db.write_batch() as signed_claims_batch:
                    with self.outpoint_to_claim_id_db.write_batch() as outpoint_batch:
                        self.flush_claims(claims_batch, names_batch, signed_claims_batch,
                                          outpoint_batch)

    def flush_claims(self, batch, names_batch, signed_claims_batch, outpoint_batch):
        flush_start = time.time()
        write_claim, write_name, write_cert = batch.put, names_batch.put, signed_claims_batch.put
        write_outpoint = outpoint_batch.put
        delete_claim, delete_outpoint, delete_name = batch.delete, outpoint_batch.delete, names_batch.delete
        delete_cert = signed_claims_batch.delete
        for claim_id, outpoints in self.pending_abandons.items():
            claim = self.get_claim_info(claim_id)
            self.remove_claim_for_name(claim.name, claim_id)
            if claim.cert_id:
                self.remove_claim_from_certificate_claims(claim.cert_id, claim_id)
            self.remove_certificate(claim_id)
            self.claim_cache[claim_id] = None
            for txid, tx_index in outpoints:
                self.put_claim_id_for_outpoint(txid, tx_index, None)
        for key, claim in self.claim_cache.items():
            if claim:
                write_claim(key, claim)
            else:
                delete_claim(key)
        for name, claims in self.claims_for_name_cache.items():
            if not claims:
                delete_name(name)
            else:
                write_name(name, msgpack.dumps(claims))
        for cert_id, claims in self.claims_signed_by_cert_cache.items():
            if not claims:
                delete_cert(cert_id)
            else:
                write_cert(cert_id, msgpack.dumps(claims))
        for key, claim_id in self.outpoint_to_claim_id_cache.items():
            if claim_id:
                write_outpoint(key, claim_id)
            else:
                delete_outpoint(key)
        self.logger.info('flushed {:,d} blocks with {:,d} claims, {:,d} outpoints, {:,d} names '
                         'and {:,d} certificates added while {:,d} were abandoned in {:.1f}s, committing...'
                         .format(self.height - self.db_height,
                                 len(self.claim_cache), len(self.outpoint_to_claim_id_cache),
                                 len(self.claims_for_name_cache),
                                 len(self.claims_signed_by_cert_cache), len(self.pending_abandons),
                                 time.time() - flush_start))
        self.claim_cache = {}
        self.claims_for_name_cache = {}
        self.claims_signed_by_cert_cache = {}
        self.outpoint_to_claim_id_cache = {}
        self.pending_abandons = {}

    def assert_flushed(self):
        super().assert_flushed()
        assert not self.claim_cache
        assert not self.claims_for_name_cache
        assert not self.claims_signed_by_cert_cache
        assert not self.outpoint_to_claim_id_cache
        assert not self.pending_abandons

    def advance_blocks(self, blocks):
        # save height, advance blocks as usual, then hook our claim tx processing
        height = self.height + 1
        super().advance_blocks(blocks)
        pending_undo = []
        for index, block in enumerate(blocks):
            undo = self.advance_claim_txs(block.transactions, height + index)
            pending_undo.append((height+index, undo,))
        with self.claim_undo_db.write_batch() as writer:
            for height, undo_info in pending_undo:
                writer.put(struct.pack(">I", height), msgpack.dumps(undo_info))

    def spend_utxo(self, tx_hash, tx_idx):
        # this is called during electrumx tx advance, we gather spents in the process to avoid looping again
        result = super().spend_utxo(tx_hash, tx_idx)
        return result

    def advance_claim_txs(self, txs, height):
        # TODO: generate claim undo info!
        undo_info = []
        add_undo = undo_info.append
        update_inputs = set()
        for tx, txid in txs:
            update_inputs.clear()
            if tx.has_claims:
                for index, output in enumerate(tx.outputs):
                    claim = output.claim
                    if isinstance(claim, NameClaim):
                        add_undo(self.advance_claim_name_transaction(output, height, txid, index))
                    elif isinstance(claim, ClaimUpdate):
                        update_input = self.get_update_input(claim, tx.inputs)
                        if update_input:
                            update_inputs.add(update_input)
                            add_undo(self.advance_update_claim(output, height, txid, index))
                        else:
                            info = (hash_to_str(txid), hash_to_str(claim.claim_id),)
                            self.log_error("REJECTED: {} updating {}".format(*info))
                    elif isinstance(claim, ClaimSupport):
                        self.advance_support(claim, txid, index, height, output.value)
            for txin in tx.inputs:
                if txin not in update_inputs:
                    abandoned_claim_id = self.abandon_spent(txin.prev_hash, txin.prev_idx)
                    if abandoned_claim_id:
                        add_undo((abandoned_claim_id, self.get_claim_info(abandoned_claim_id)))
        return undo_info

    def advance_update_claim(self, output, height, txid, nout):
        claim_id = output.claim.claim_id
        claim_info = self.claim_info_from_output(output, txid, nout, height)
        old_claim_info = self.get_claim_info(claim_id)
        self.put_claim_id_for_outpoint(old_claim_info.txid, old_claim_info.nout, None)
        if old_claim_info.cert_id:
            self.remove_claim_from_certificate_claims(old_claim_info.cert_id, claim_id)
        if claim_info.cert_id:
            self.put_claim_id_signed_by_cert_id(claim_info.cert_id, claim_id)
        self.put_claim_info(claim_id, claim_info)
        self.put_claim_id_for_outpoint(txid, nout, claim_id)
        return claim_id, old_claim_info

    def advance_claim_name_transaction(self, output, height, txid, nout):
        claim_id = claim_id_hash(txid, nout)
        claim_info = self.claim_info_from_output(output, txid, nout, height)
        if claim_info.cert_id:
            self.put_claim_id_signed_by_cert_id(claim_info.cert_id, claim_id)
        self.put_claim_info(claim_id, claim_info)
        self.put_claim_for_name(claim_info.name, claim_id)
        self.put_claim_id_for_outpoint(txid, nout, claim_id)
        return claim_id, None

    def backup_from_undo_info(self, claim_id, undo_claim_info):
        """
        Undo information holds a claim state **before** a transaction changes it
        There are 4 possibilities when processing it, of which only 3 are valid ones:
         1. the claim is known and the undo info has info, it was an update
         2. the claim is known and the undo info doesn't hold any info, it was claimed
         3. the claim in unknown and the undo info has info, it was abandoned
         4. the claim is unknown and the undo info does't hold info, error!
        """

        undo_claim_info = ClaimInfo(*undo_claim_info) if undo_claim_info else None
        current_claim_info = self.get_claim_info(claim_id)
        if current_claim_info and undo_claim_info:
            # update, remove current claim
            self.remove_claim_id_for_outpoint(current_claim_info.txid, current_claim_info.nout)
            if current_claim_info.cert_id:
                self.remove_claim_from_certificate_claims(current_claim_info.cert_id, claim_id)
        elif current_claim_info and not undo_claim_info:
            # claim, abandon it
            self.abandon_spent(current_claim_info.txid, current_claim_info.nout)
        elif not current_claim_info and undo_claim_info:
            # abandon, reclaim it (happens below)
            pass
        else:
            # should never happen, unless the database got into an inconsistent state
            raise Exception("Unexpected situation occurred on backup, this means the database is inconsistent. "
                            "Please report. Resetting the data folder (reindex) solves it for now.")
        if undo_claim_info:
            self.put_claim_info(claim_id, undo_claim_info)
            if undo_claim_info.cert_id:
                cert_id = self._checksig(undo_claim_info.name, undo_claim_info.value, undo_claim_info.address)
                self.put_claim_id_signed_by_cert_id(cert_id, claim_id)
            self.put_claim_for_name(undo_claim_info.name, claim_id)
            self.put_claim_id_for_outpoint(undo_claim_info.txid, undo_claim_info.nout, claim_id)

    def backup_txs(self, txs):
        self.log_info("Reorg at height {} with {} transactions.".format(self.height, len(txs)))
        undo_info = msgpack.loads(self.claim_undo_db.get(struct.pack(">I", self.height)), use_list=False)
        for claim_id, undo_claim_info in reversed(undo_info):
            self.backup_from_undo_info(claim_id, undo_claim_info)
        return super().backup_txs(txs)

    def backup_blocks(self, raw_blocks):
        self.batched_flush_claims()
        super().backup_blocks(raw_blocks=raw_blocks)
        self.batched_flush_claims()

    def shutdown(self, executor):
        self.batched_flush_claims()
        return super().shutdown(executor=executor)

    def backup_claim_name(self, txid, nout):
        self.abandon_spent(txid, nout)

    def advance_support(self, claim_support, txid, nout, height, amount):
        # TODO: check for more controller claim rules, like takeover or ordering
        pass

    def claim_info_from_output(self, output, txid, nout, height):
        amount = output.value
        address = self.coin.address_from_script(output.pk_script)
        name, value, cert_id = output.claim.name, output.claim.value, None
        assert txid and address
        cert_id = self._checksig(name, value, address)
        return ClaimInfo(name, value, txid, nout, amount, address, height, cert_id)

    def _checksig(self, name, value, address):
        try:
            parse_lbry_uri(name.decode())  # skip invalid names
            cert_id = Claim.FromString(value).publisherSignature.certificateId[::-1] or None
            if not self.should_validate_signatures:
                return cert_id
            if cert_id:
                cert_claim = self.get_claim_info(cert_id)
                if cert_claim:
                    certificate = smart_decode(cert_claim.value)
                    claim_dict = smart_decode(value)
                    claim_dict.validate_signature(address, certificate)
                    return cert_id
        except Exception as e:
            pass

    def get_update_input(self, claim, inputs):
        claim_id = claim.claim_id
        claim_info = self.get_claim_info(claim_id)
        if not claim_info:
            return False
        for input in inputs:
            if input.prev_hash == claim_info.txid and input.prev_idx == claim_info.nout:
                return input
        return False

    def abandon_spent(self, tx_hash, tx_idx):
        claim_id = self.get_claim_id_from_outpoint(tx_hash, tx_idx)
        if claim_id:
            self.log_info("[!] Abandon: {}".format(hash_to_str(claim_id)))
            self.pending_abandons.setdefault(claim_id, []).append((tx_hash, tx_idx,))
            return claim_id

    def put_claim_id_for_outpoint(self, tx_hash, tx_idx, claim_id):
        self.log_info("[+] Adding outpoint: {}:{} for {}.".format(hash_to_str(tx_hash), tx_idx,
                                                                  hash_to_str(claim_id) if claim_id else None))
        self.outpoint_to_claim_id_cache[tx_hash + struct.pack('>I', tx_idx)] = claim_id

    def remove_claim_id_for_outpoint(self, tx_hash, tx_idx):
        self.log_info("[-] Remove outpoint: {}:{}.".format(hash_to_str(tx_hash), tx_idx))
        self.outpoint_to_claim_id_cache[tx_hash + struct.pack('>I', tx_idx)] = None

    def get_claim_id_from_outpoint(self, tx_hash, tx_idx):
        key = tx_hash + struct.pack('>I', tx_idx)
        return self.outpoint_to_claim_id_cache.get(key) or self.outpoint_to_claim_id_db.get(key)

    def get_claims_for_name(self, name):
        if name in self.claims_for_name_cache: return self.claims_for_name_cache[name]
        db_claims = self.names_db.get(name)
        return msgpack.loads(db_claims) if db_claims else {}

    def put_claim_for_name(self, name, claim_id):
        self.log_info("[+] Adding claim {} for name {}.".format(hash_to_str(claim_id), name))
        claims = self.get_claims_for_name(name)
        claims.setdefault(claim_id, max(claims.values() or [0]) + 1)
        self.claims_for_name_cache[name] = claims

    def remove_claim_for_name(self, name, claim_id):
        self.log_info("[-] Removing claim from name: {} - {}".format(hash_to_str(claim_id), name))
        claims = self.get_claims_for_name(name)
        claim_n = claims.pop(claim_id)
        for claim_id, number in claims.items():
            if number > claim_n:
                claims[claim_id] = number - 1
        self.claims_for_name_cache[name] = claims

    def get_signed_claim_ids_by_cert_id(self, cert_id):
        if cert_id in self.claims_signed_by_cert_cache: return self.claims_signed_by_cert_cache[cert_id]
        db_claims = self.signatures_db.get(cert_id)
        return msgpack.loads(db_claims, use_list=True) if db_claims else []

    def put_claim_id_signed_by_cert_id(self, cert_id, claim_id):
        self.log_info("[+] Adding signature: {} - {}".format(hash_to_str(claim_id), hash_to_str(cert_id)))
        certs = self.get_signed_claim_ids_by_cert_id(cert_id)
        certs.append(claim_id)
        self.claims_signed_by_cert_cache[cert_id] = certs

    def remove_certificate(self, cert_id):
        self.log_info("[-] Removing certificate: {}".format(hash_to_str(cert_id)))
        self.claims_signed_by_cert_cache[cert_id] = []

    def remove_claim_from_certificate_claims(self, cert_id, claim_id):
        self.log_info("[-] Removing signature: {} - {}".format(hash_to_str(claim_id), hash_to_str(cert_id)))
        certs = self.get_signed_claim_ids_by_cert_id(cert_id)
        if claim_id in certs:
            certs.remove(claim_id)
        self.claims_signed_by_cert_cache[cert_id] = certs

    def get_claim_info(self, claim_id):
        serialized = self.claim_cache.get(claim_id) or self.claims_db.get(claim_id)
        return ClaimInfo.from_serialized(serialized) if serialized else None

    def put_claim_info(self, claim_id, claim_info):
        self.log_info("[+] Adding claim info for: {}".format(hash_to_str(claim_id)))
        self.claim_cache[claim_id] = claim_info.serialized

def claim_id_hash(txid, n):
    # TODO: This should be in lbryschema
    packed = txid + struct.pack('>I', n)
    md = hashlib.new('ripemd160')
    md.update(hashlib.sha256(packed).digest())
    return md.digest()
