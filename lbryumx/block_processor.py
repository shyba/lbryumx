import hashlib
import struct
import time
from typing import Tuple

import msgpack
from electrumx.lib.hash import hash_to_str

from electrumx.server.block_processor import BlockProcessor
from lbryschema.decode import smart_decode
from lbryschema.uri import parse_lbry_uri

from lbryumx.model import NameClaim, ClaimInfo, ClaimUpdate, ClaimSupport


class LBRYBlockProcessor(BlockProcessor):

    def __init__(self, *args, **kwargs):
        self.claim_cache = {}
        self.supports_cache = {}
        self.claims_for_name_cache = {}
        self.claims_signed_by_cert_cache = {}
        self.outpoint_to_claim_id_cache = {}
        self.claims_db = self.names_db = self.signatures_db = self.outpoint_to_claim_id_db = self.supports_db = None
        super().__init__(*args, **kwargs)

        # stores deletes not yet flushed to disk
        self.pending_abandons = {}

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
                self.supports_db.close()
                self.names_db.close()
                self.signatures_db.close()
                self.outpoint_to_claim_id_db.close()
            self.claims_db = self.db_class('claims', for_sync)
            self.supports_db = self.db_class('supports', for_sync)
            self.names_db = self.db_class('names', for_sync)
            self.signatures_db = self.db_class('signatures', for_sync)
            self.outpoint_to_claim_id_db = self.db_class('outpoint_claim_id', for_sync)
            log_reason('opened claim DBs', self.claims_db.for_sync)

    def flush_utxos(self, utxo_batch):
        # flush claims together with utxos as they are parsed together
        self.batched_flush_claims()
        return super().flush_utxos(utxo_batch)

    def batched_flush_claims(self):
        with self.claims_db.write_batch() as claims_batch:
            with self.names_db.write_batch() as names_batch:
                with self.signatures_db.write_batch() as signed_claims_batch:
                    with self.outpoint_to_claim_id_db.write_batch() as outpoint_batch:
                        with self.supports_db.write_batch() as supports_batch:
                            self.flush_claims(claims_batch, names_batch, signed_claims_batch,
                                              outpoint_batch, supports_batch)

    def flush_claims(self, batch, names_batch, signed_claims_batch, outpoint_batch, supports_batch):
        flush_start = time.time()
        write_claim, write_name, write_cert = batch.put, names_batch.put, signed_claims_batch.put
        write_outpoint, write_support = outpoint_batch.put, supports_batch.put
        delete_claim, delete_outpoint, delete_name = batch.delete, outpoint_batch.delete, names_batch.delete
        delete_cert, delete_support = signed_claims_batch.delete, supports_batch.delete
        for claim_id, outpoints in self.pending_abandons.items():
            if not outpoints: continue
            claim = self.get_claim_info(claim_id)
            self.remove_claim_for_name(claim.name, claim_id)
            if claim.cert_id:
                self.remove_claim_from_certificate_claims(claim.cert_id, claim_id)
            self.remove_certificate(claim_id)
            if claim_id in self.claim_cache:
                del self.claim_cache[claim_id]
            delete_claim(claim_id)
            for txid, tx_index in outpoints:
                outpoint = txid + struct.pack('>I', tx_index)
                if outpoint in self.outpoint_to_claim_id_cache:
                    del self.outpoint_to_claim_id_cache[outpoint]
                delete_outpoint(outpoint)
        for key, claim in self.claim_cache.items():
            write_claim(key, claim)
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
            write_outpoint(key, claim_id)
        for key, value in self.supports_cache.items():
            if value:
                write_support(key, value)
            else:
                delete_support(key)
        if self.claims_db.for_sync:
            self.logger.info('flushed {:,d} blocks with {:,d} claims, {:,d} outpoints, {:,d} names '
                             'and {:,d} certificates added while {:,d} were abandoned in {:.1f}s, committing...'
                             .format(self.height - self.db_height,
                                     len(self.claim_cache), len(self.outpoint_to_claim_id_cache),
                                     len(self.claims_for_name_cache),
                                     len(self.claims_signed_by_cert_cache), len(self.pending_abandons),
                                     time.time() - flush_start))
        self.claim_cache = {}
        self.supports_cache = {}
        self.claims_for_name_cache = {}
        self.claims_signed_by_cert_cache = {}
        self.outpoint_to_claim_id_cache = {}
        self.pending_abandons = {}

    def assert_flushed(self):
        super().assert_flushed()
        assert not self.claim_cache
        assert not self.supports_cache
        assert not self.claims_for_name_cache
        assert not self.claims_signed_by_cert_cache
        assert not self.outpoint_to_claim_id_cache
        assert not self.pending_abandons

    def advance_txs(self, txs):
        # TODO: generate claim undo info!
        undo_info = super().advance_txs(txs)
        height = self.height + 1
        for tx, txid in txs:
            if tx.has_claims:
                for index, output in enumerate(tx.outputs):
                    claim = output.claim
                    if isinstance(claim, NameClaim):
                        self.advance_claim_name_transaction(output, height, txid, index)
                    if isinstance(claim, ClaimUpdate):
                        if self.is_update_valid(claim, tx.inputs):
                            self.update_claim(output, height, txid, index)
                        else:
                            info = (hash_to_str(txid), hash_to_str(claim.claim_id),)
                            self.log_error("REJECTED: {} updating {}".format(*info))
                    if isinstance(claim, ClaimSupport):
                        self.advance_support(self, tx.inputs, claim, txid, index, height, output.value)
        return undo_info

    def update_claim(self, output, height, txid, nout):
        claim_id = output.claim.claim_id
        if self.pending_abandons.get(claim_id):
            old_claim_info = self.get_claim_info(claim_id)
            outpoint = (old_claim_info.txid, old_claim_info.nout,)
            self.pending_abandons[claim_id].remove(outpoint)
        claim_info = self.claim_info_from_output(output, txid, nout, height)
        self.put_claim_info(claim_id, claim_info)
        self.put_claim_id_for_outpoint(txid, nout, claim_id)

    def advance_claim_name_transaction(self, output, height, txid, nout):
        claim_id = claim_id_hash(txid, nout)
        claim_info = self.claim_info_from_output(output, txid, nout, height)
        if claim_info.cert_id:
            self.put_claim_id_signed_by_cert_id(claim_info.cert_id, claim_id)
        self.put_claim_info(claim_id, claim_info)
        self.put_claim_for_name(claim_info.name, claim_id)
        self.put_claim_id_for_outpoint(txid, nout, claim_id)

    def advance_support(self, inputs, claim_support, txid, nout, height, amount):
        for input in inputs:
            if input.prev_hash == txid and input.prev_idx == nout:
                # TODO: also applies to the other types. Refactor and add more tests, specially before undo code lands
                return  # its spent, so an in-block support+abandon, which we then ignore
        self.put_support(claim_support.name, claim_support.claim_id, txid, nout, height, amount)

    def claim_info_from_output(self, output, txid, nout, height):
        amount = output.value
        address = self.coin.address_from_script(output.pk_script)
        name, value, cert_id = output.claim.name, output.claim.value, None
        try:
            parse_lbry_uri(name.decode())  # skip invalid names
            cert_id = smart_decode(value).certificate_id
        except Exception:
            pass
        return ClaimInfo(name, value, txid, nout, amount, address, height, cert_id)

    def is_update_valid(self, claim, inputs):
        claim_id = claim.claim_id
        claim_info = self.get_claim_info(claim_id)
        if not claim_info:
            return False
        for input in inputs:
            if input.prev_hash == claim_info.txid and input.prev_idx == claim_info.nout:
                return True
        return False

    def spend_utxo(self, tx_hash, tx_idx):
        # gather pending abandons during the spend of a utxo
        result = super().spend_utxo(tx_hash, tx_idx)
        self.abandon_spent(tx_hash, tx_idx)
        return result

    def abandon_spent(self, tx_hash, tx_idx):
        claim_id = self.get_claim_id_from_outpoint(tx_hash, tx_idx)
        if claim_id:
            self.pending_abandons.setdefault(claim_id, []).append((tx_hash, tx_idx,))

    def put_claim_id_for_outpoint(self, tx_hash, tx_idx, claim_id):
        self.outpoint_to_claim_id_cache[tx_hash + struct.pack('>I', tx_idx)] = claim_id

    def get_claim_id_from_outpoint(self, tx_hash, tx_idx):
        key = tx_hash + struct.pack('>I', tx_idx)
        return self.outpoint_to_claim_id_cache.get(key) or self.outpoint_to_claim_id_db.get(key)

    def get_claims_for_name(self, name):
        if name in self.claims_for_name_cache: return self.claims_for_name_cache[name]
        db_claims = self.names_db.get(name)
        return msgpack.loads(db_claims) if db_claims else {}

    def put_claim_for_name(self, name, claim_id):
        claims = self.get_claims_for_name(name)
        claims[claim_id] = max(claims.values() or [0]) + 1
        self.claims_for_name_cache[name] = claims

    def remove_claim_for_name(self, name, claim_id):
        claims = self.get_claims_for_name(name)
        claim_n = claims.pop(claim_id)
        for claim_id, number in claims.items():
            if number > claim_n:
                claims[claim_id] = number - 1
        self.claims_for_name_cache[name] = claims

    def get_signed_claim_id_by_cert_id(self, cert_id):
        if cert_id in self.claims_signed_by_cert_cache: return self.claims_signed_by_cert_cache[cert_id]
        db_claims = self.signatures_db.get(cert_id)
        return msgpack.loads(db_claims, use_list=False) if db_claims else tuple()

    def put_claim_id_signed_by_cert_id(self, cert_id, claim_id):
        self.claims_signed_by_cert_cache[cert_id] = self.get_signed_claim_id_by_cert_id(cert_id) + (claim_id,)

    def remove_certificate(self, cert_id):
        self.claims_signed_by_cert_cache[cert_id] = tuple()

    def remove_claim_from_certificate_claims(self, cert_id, claim_id):
        certs = self.get_signed_claim_id_by_cert_id(cert_id)
        certs = tuple(filter(lambda claimed_id: claimed_id != claim_id, certs))
        self.claims_signed_by_cert_cache[cert_id] = certs

    def get_claim_info(self, claim_id):
        serialized = self.claim_cache.get(claim_id) or self.claims_db.get(claim_id)
        return ClaimInfo.from_serialized(serialized) if serialized else None

    def put_claim_info(self, claim_id, claim_info):
        self.claim_cache[claim_id] = claim_info.serialized

    def get_supported_claim_name_id_from_outpoint(self, txid, nout):
        outpoint = txid + struct.pack('>I', nout)
        support = self.supports_cache.get(outpoint) or self.supports_db.get(outpoint)
        return msgpack.loads(support, use_list=False) if support else None

    def get_supports_for_name(self, name):
        supports = self.supports_cache.get(name) or self.supports_db.get(name)
        return msgpack.loads(supports) if supports else None

    def put_support(self, name, claim_id, txid, nout, height, amount):
        supports = self.get_supports_for_name(name) or {}
        supports.setdefault(claim_id, []).append((txid, nout, height, amount))
        self.supports_cache[name] = msgpack.dumps(supports)
        outpoint = txid + struct.pack('>I', nout)
        self.supports_cache[outpoint] = msgpack.dumps((name, claim_id,))

    def remove_support_outpoint(self, txid, nout):
        outpoint = txid + struct.pack('>I', nout)
        supported = self.get_supported_claim_name_id_from_outpoint(txid, nout)
        if supported:
            name, claim_id = supported
            self.supports_cache[outpoint] = None

            def non_matching(support):
                existing_txid, existing_nout = support[:2]
                return existing_txid != txid and existing_nout != nout
            supports = self.get_supports_for_name(name)
            supports[claim_id] = list(filter(non_matching, supports[claim_id]))
            self.supports_cache[name] = msgpack.dumps(supports)

def claim_id_hash(txid, n):
    # TODO: This should be in lbryschema
    packed = txid + struct.pack('>I', n)
    md = hashlib.new('ripemd160')
    md.update(hashlib.sha256(packed).digest())
    return md.digest()
