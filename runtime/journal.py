"""V3 decision journal: exact replay, durable hash chain, committed chain head.

This format is intentionally independent of historical experiment journals. A
successful response is replayed only if its actual bytes and request survived;
an unknown in-flight request is delegated to the backend's durable attempt
ledger, never treated as success or as a new retry allowance.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

PROTOCOL = 'full-workflow-decision-journal-v3'
ZERO = '0' * 64


class JournalError(RuntimeError):
    def __init__(self, code, message='Decision journal validation failed.'):
        self.code = code
        super().__init__(message)


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _read_json(raw):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError('duplicate_key')
            out[key] = value
        return out
    def constant(_):
        raise ValueError('nonfinite_number')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def _validate_response(value, schema):
    try:
        from court_agent.llm import _validate_value
    except ModuleNotFoundError:
        import backends
        backends.bootstrap()
        from court_agent.llm import _validate_value
    try:
        return _validate_value(value, schema, schema)
    except (KeyError, TypeError, ValueError, RecursionError):
        return False


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class HashChainedDecisionJournal:
    """Drop-in ``DecisionJournal`` API for newly created v3 runs only.

    The caller must hold the run lock. ``<path>.head.json`` binds the last fully
    committed chain record and detects valid-prefix truncation. On a crash
    between the journal fsync and head replacement, exactly one validated
    successor record may advance the head. Neither truncated records nor a
    missing checkpoint for an established journal are repaired.
    """
    def __init__(self, backend, path, *, resume=False):
        self.backend, self.path = backend, Path(path)
        self.head_path = self.path.with_name(self.path.name + '.head.json')
        self.cursor = self.replayed = self.new_calls = 0
        self.saved = {}
        self.rows = []
        self.previous_hash = ZERO
        self.byte_count = 0
        self.journal_id = None
        if self.path.is_symlink() or self.head_path.is_symlink():
            raise JournalError('journal_symlink_rejected')
        if self.path.exists():
            if not resume:
                raise JournalError('journal_exists')
            self._load()
        else:
            if self.head_path.exists():
                raise JournalError('journal_missing_with_checkpoint')
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.journal_id = uuid.uuid4().hex
            self._append({'kind': 'policy', 'protocol': PROTOCOL, 'journal_id': self.journal_id})

    def _load(self):
        try:
            raw = self.path.read_bytes()
            if not raw or not raw.endswith(b'\n'):
                raise JournalError('journal_truncated')
            active = None
            sizes = []
            for line in raw.splitlines(keepends=True):
                row = _read_json(line.decode('utf-8'))
                if not isinstance(row, dict):
                    raise JournalError('journal_invalid_record')
                payload = {k: v for k, v in row.items() if k != 'record_sha256'}
                if (type(row.get('sequence')) is not int or row['sequence'] != len(self.rows) + 1
                        or row.get('previous_sha256') != self.previous_hash
                        or row.get('record_sha256') != _hash(payload)):
                    raise JournalError('journal_hash_mismatch')
                kind = row.get('kind')
                if not self.rows:
                    if (kind != 'policy' or row.get('protocol') != PROTOCOL
                            or not isinstance(row.get('journal_id'), str) or len(row['journal_id']) != 32):
                        raise JournalError('journal_protocol_mismatch')
                    self.journal_id = row['journal_id']
                elif kind == 'request':
                    index = row.get('logical_decision')
                    request = row.get('request')
                    if (type(index) is not int or index < 1 or not isinstance(request, dict)
                            or set(request) != {'role_id', 'system', 'context', 'schema'}
                            or not isinstance(request['role_id'], str) or not request['role_id']
                            or not isinstance(request['system'], str)
                            or not isinstance(request['context'], dict)
                            or not isinstance(request['schema'], dict)
                            or row.get('request_hash') != _hash(request)):
                        raise JournalError('journal_invalid_request')
                    old = self.saved.get(index)
                    if old:
                        if (index != len(self.saved) or 'response' in old
                                or old['request_hash'] != row['request_hash'] or old['request'] != request):
                            raise JournalError('journal_request_changed')
                    elif (index != len(self.saved) + 1
                          or (self.saved and 'response' not in self.saved[index - 1])):
                        raise JournalError('journal_prefix_incomplete')
                    self.saved.setdefault(index, deepcopy(row))
                    active = index
                elif kind in {'response', 'error'}:
                    index = row.get('logical_decision')
                    if type(index) is not int or index != active or index not in self.saved:
                        raise JournalError('journal_response_without_unique_request')
                    old = self.saved[index]
                    if 'response' in old or row.get('request_hash') != old['request_hash']:
                        raise JournalError('journal_response_binding_mismatch')
                    if kind == 'response':
                        if ('response' not in row or row.get('response_sha256') != _hash(row['response'])
                                or not _validate_response(row['response'], old['request']['schema'])):
                            raise JournalError('cached_response_schema_or_hash_invalid')
                        old['response'] = deepcopy(row['response'])
                    elif not isinstance(row.get('error_code'), str) or not row['error_code']:
                        raise JournalError('journal_invalid_error')
                    active = None
                else:
                    raise JournalError('journal_invalid_record_kind')
                self.rows.append(row)
                self.previous_hash = row['record_sha256']
                self.byte_count += len(line)
                sizes.append(self.byte_count)
            self._check_head(sizes)
        except JournalError:
            raise
        except (OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError):
            raise JournalError('journal_invalid') from None

    def _head_value(self):
        return {'protocol': PROTOCOL, 'journal_id': self.journal_id,
                'record_count': len(self.rows), 'record_sha256': self.previous_hash,
                'byte_count': self.byte_count}

    def _check_head(self, sizes):
        if not self.head_path.exists():
            # The first policy record may have been fsynced before a crash.
            if len(self.rows) == 1:
                self._write_head()
                return
            raise JournalError('journal_checkpoint_missing')
        head = _read_json(self.head_path.read_text(encoding='utf-8'))
        if not isinstance(head, dict) or set(head) != set(self._head_value()):
            raise JournalError('journal_checkpoint_invalid')
        count = head.get('record_count')
        if (head.get('protocol') != PROTOCOL or head.get('journal_id') != self.journal_id
                or type(count) is not int or count < 1 or count > len(self.rows)
                or self.rows[count - 1]['record_sha256'] != head.get('record_sha256')
                or type(head.get('byte_count')) is not int or sizes[count - 1] != head['byte_count']):
            raise JournalError('journal_checkpoint_mismatch')
        if count == len(self.rows):
            return
        if count != len(self.rows) - 1:
            raise JournalError('journal_checkpoint_gap')
        self._write_head()

    def _write_head(self):
        if self.head_path.is_symlink():
            raise JournalError('journal_symlink_rejected')
        fd, name = tempfile.mkstemp(prefix='.' + self.head_path.name + '-', dir=self.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(self._head_value(), stream, ensure_ascii=False, allow_nan=False,
                          sort_keys=True, separators=(',', ':'))
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.head_path)
            _fsync_dir(self.path.parent)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _append(self, payload):
        row = {**deepcopy(payload), 'sequence': len(self.rows) + 1,
               'previous_sha256': self.previous_hash}
        row['record_sha256'] = _hash(row)
        raw = (json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n').encode()
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'ab') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        self.rows.append(row)
        self.previous_hash = row['record_sha256']
        self.byte_count += len(raw)
        self._write_head()

    @property
    def stats(self):
        return {**deepcopy(getattr(self.backend, 'stats', {})),
                'replayed_decisions': self.replayed, 'new_decisions': self.new_calls,
                'journal_protocol': PROTOCOL, 'journal_record_count': len(self.rows),
                'journal_record_sha256': self.previous_hash}

    @property
    def calls(self):
        return getattr(self.backend, 'calls', [])

    def decide(self, role_id, system, context, schema):
        request = deepcopy({'role_id': role_id, 'system': system, 'context': context, 'schema': schema})
        self.cursor += 1
        old = self.saved.get(self.cursor)
        digest = _hash(request)
        if old and (old['request_hash'] != digest or old['request'] != request):
            raise JournalError('replay_request_mismatch', f'Logical decision {self.cursor}; no new call made.')
        if old and 'response' in old:
            if not _validate_response(old['response'], schema):
                raise JournalError('cached_response_schema_invalid')
            if getattr(self.backend, 'is_offline_fixture', False):
                # A stateful fixture behind ReliableBackend advances its local
                # counters directly; cached replay must not reserve another call.
                advance = getattr(self.backend, 'replay_cached_decision', self.backend.decide)
                advance(**deepcopy(request))
            self.replayed += 1
            return deepcopy(old['response'])
        if self.cursor < len(self.saved):
            raise JournalError('replay_gap')
        self._append({'kind': 'request', 'logical_decision': self.cursor,
                      'request_hash': digest, 'request': request})
        self.saved.setdefault(self.cursor, {'request_hash': digest, 'request': deepcopy(request)})
        self.new_calls += 1
        try:
            response = self.backend.decide(**deepcopy(request))
        except BaseException as exc:
            code = getattr(exc, 'code', 'interrupted' if isinstance(exc, KeyboardInterrupt) else type(exc).__name__)
            self._append({'kind': 'error', 'logical_decision': self.cursor,
                          'request_hash': digest, 'error_code': str(code)})
            raise
        if not _validate_response(response, schema):
            self._append({'kind': 'error', 'logical_decision': self.cursor,
                          'request_hash': digest, 'error_code': 'new_response_schema_invalid'})
            raise JournalError('new_response_schema_invalid')
        # Local persistence failures must not be mislabelled as provider failure.
        self._append({'kind': 'response', 'logical_decision': self.cursor,
                      'request_hash': digest, 'response_sha256': _hash(response),
                      'response': deepcopy(response), 'stats': deepcopy(getattr(self.backend, 'stats', {}))})
        self.saved[self.cursor]['response'] = deepcopy(response)
        return deepcopy(response)

    def ensure_replayed(self):
        if self.cursor < len(self.saved):
            raise JournalError('unconsumed_cached_decisions')

    def close(self):
        if hasattr(self.backend, 'close'):
            self.backend.close()


DecisionJournal = HashChainedDecisionJournal
