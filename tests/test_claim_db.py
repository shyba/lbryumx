import json
from binascii import hexlify

from electrumx.lib.hash import hash_to_str

from lbryumx.model import ClaimInfo


def test_claim_sequence_remove_reorders(block_processor):
    name, db = b'name', block_processor
    db.put_claim_for_name(name, 'id1')
    db.put_claim_for_name(name, 'id2')
    db.put_claim_for_name(name, 'id3')
    db.remove_claim_for_name(name, 'id2')

    assert db.get_claims_for_name(name) == {'id1': 1, 'id3': 2}


def test_cert_to_claims_storage(block_processor):
    db = block_processor
    db.put_claim_id_signed_by_cert_id(b'certificate_id', b'claim_id1')
    db.put_claim_id_signed_by_cert_id(b'certificate_id2', b'claim_id2')
    assert db.get_signed_claim_id_by_cert_id(b'certificate_id') == (b'claim_id1',)


def test_cert_to_claims_storage_removal_of_certificate(block_processor):
    db = block_processor
    db.put_claim_id_signed_by_cert_id(b'certificate_id', b'claim_id1')
    db.put_claim_id_signed_by_cert_id(b'certificate_id', b'claim_id2')
    db.remove_certificate(b'certificate_id')
    assert db.get_signed_claim_id_by_cert_id(b'certificate_id') == ()


def test_cert_to_claims_storage_removal_of_claim_id(block_processor):
    db = block_processor
    db.put_claim_id_signed_by_cert_id(b'certificate_id', b'claim_id1')
    db.put_claim_id_signed_by_cert_id(b'certificate_id', b'claim_id2')
    db.remove_claim_from_certificate_claims(b'certificate_id', b'claim_id1')
    assert db.get_signed_claim_id_by_cert_id(b'certificate_id') == (b'claim_id2',)


def test_claim_id_outpoint_retrieval(block_processor):
    db = block_processor
    db.put_claim_id_for_outpoint(b'txid bytes', tx_idx=2, claim_id=b'400cafe800')
    assert db.get_claim_id_from_outpoint(b'txid bytes', tx_idx=2) == b'400cafe800'


def test_pending_abandons_trigger(block_processor):
    block_processor.abandon_spent(b'inexistent_tx', 2)
    assert not block_processor.pending_abandons
    block_processor.put_claim_id_for_outpoint(b'existing_tx', tx_idx=4, claim_id=b'1337')
    block_processor.abandon_spent(b'existing_tx', 4)
    assert b'1337' in block_processor.pending_abandons


def test_supports_storage(block_processor):
    db = block_processor
    name = b'supportName'
    claim_id, txid, nout, height, amount = b'claim_id', b'txid', 12, 400, 4000
    assert not db.get_supported_claim_name_id_from_outpoint(txid, nout)
    assert not db.get_supports_for_name(name)

    db.put_support(name, claim_id, txid, nout, height, amount)

    assert db.get_supported_claim_name_id_from_outpoint(txid, nout) == (name, claim_id,)
    assert db.get_supports_for_name(name) == {claim_id: [[txid, nout, height, amount]]}

    db.remove_support_outpoint(txid, nout)

    assert not db.get_supported_claim_name_id_from_outpoint(txid, nout)
    assert db.get_supports_for_name(name) == {claim_id: []}

    db.put_support(name, claim_id, txid, nout, height, amount)
    db.put_support(name, claim_id, b'othertxid', nout*2, height*2, amount)
    db.put_support(name, b'otherclaimid', b'othertxid', nout, height, amount*4)
    db.put_support(b'othername', b'yetotherclaimid', b'yetothertxid', nout, height, amount)

    assert db.get_supports_for_name(name) == {claim_id: [[txid, nout, height, amount],
                                                         [b'othertxid', nout*2, height*2, amount]],
                                              b'otherclaimid': [[b'othertxid', nout, height, amount*4]]}

def test_supports_cache_layer_flushes_to_disk_properly(block_processor):
    db = block_processor
    name = b'supportName'
    claim_id, txid, nout, height, amount = b'claim_id', b'txid', 12, 400, 4000
    assert not db.get_supported_claim_name_id_from_outpoint(txid, nout)
    assert not db.get_supports_for_name(name)

    db.put_support(name, claim_id, txid, nout, height, amount)
    db.batched_flush_claims()

    assert db.get_supported_claim_name_id_from_outpoint(txid, nout) == (name, claim_id,)
    assert db.get_supports_for_name(name) == {claim_id: [[txid, nout, height, amount]]}

    db.remove_support_outpoint(txid, nout)
    db.batched_flush_claims()

    assert not db.get_supported_claim_name_id_from_outpoint(txid, nout)
    assert db.get_supports_for_name(name) == {claim_id: []}

    db.put_support(name, claim_id, txid, nout, height, amount)
    db.batched_flush_claims()
    db.put_support(name, claim_id, b'othertxid', nout*2, height*2, amount)
    db.put_support(name, b'otherclaimid', b'othertxid', nout, height, amount*4)
    db.batched_flush_claims()
    db.put_support(b'othername', b'yetotherclaimid', b'yetothertxid', nout, height, amount)

    assert db.get_supports_for_name(name) == {claim_id: [[txid, nout, height, amount,],
                                                         [b'othertxid', nout*2, height*2, amount]],
                                              b'otherclaimid': [[b'othertxid', nout, height, amount*4]]}


def test_stratum_formatted_get_claim_info(block_processor):
    db = block_processor
    db.db_height = 100
    claim_id, txid = b'a'*40, b'b'*64
    db.put_claim_info(claim_id, ClaimInfo(b'name', b'value', txid, 12, 20, b'address', 10, None))
    db.put_claim_for_name(b'name', claim_id)
    db.put_support(b'name', claim_id, b'othertxid', nout=12, height=80, amount=1200)
    assert db.get_stratum_claim_info_from_raw_claim_id(claim_id) == {
        'address': 'address',
        'amount': 20,
        'claim_id': hash_to_str(claim_id),
        'claim_sequence': 1,
        'depth': 90,
        'effective_amount': 20 + 1200,
        'name': 'name',
        'nout': 12,
        'height': 10,
        'supports': [[hash_to_str(b'othertxid'), 12, 1200]],
        'txid': hash_to_str(txid),
        'valid_at_height': 10,
        'value': hexlify(b'value').decode('ISO-8859-1')
    }
    json.dumps(db.get_stratum_claim_info_from_raw_claim_id(claim_id))
