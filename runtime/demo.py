"""Zero-network deterministic plumbing fixture. Never model performance."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import backends
backends.bootstrap()
from court_agent.mock import MockBackend
from court_agent.schema import action


class WorkflowMockBackend(MockBackend):
    is_offline_fixture = True

    def __init__(self, case, candidate):
        super().__init__()
        self.case, self.candidate = case, candidate
        self.workflow_turns = {}
        work = case.path.parents[1] / 'workflows' / case.case_id / 'workflow.json'
        spec = json.loads(work.read_text(encoding='utf-8'))
        self.file_ids = [f['file_id'] for f in spec['files'] if f['file_id'].startswith('m')]
        self.service_id = spec['services'][0]['service_id']
        self.sequence = [action('list_files')]
        self.sequence += [action('read_document', target=f) for f in self.file_ids]
        self.sequence += [
            action('search_law', query='证据 来源 证明', scope='law'),
            action('request_investigation', target=self.service_id,
                   text='离线流程测试：请按冻结规则核验所供资料及缺口，保留未知。', references=['M01-P001']),
            action('write_artifact', target='case_timeline.csv',
                   text='order,event,status,source\n1,离线读档动作,仅流程测试,M01-P001\n', references=['M01-P001']),
            action('write_artifact', target='evidence_register.csv',
                   text='material_id,source_kind,status\nM01,来源摘要,离线占位非模型分析\n', references=['M01-P001']),
            action('write_artifact', target='pretrial_plan.md',
                   text='# 离线准备计划\n\n此文件只验证读写和来源链接，不是模型的法律意见或标准答案。\n来源：M01-P001。\n已执行资料核验，具体结果以工具反馈记录为准；不得编造缺失原件。', references=['M01-P001']),
            action('write_artifact', target='examination_outline.md',
                   text='# 离线询问提纲\n\n此文件只验证产物保存。应在真实运行中由候选律师自行生成具体问题。\n来源：M01-P001。', references=['M01-P001']),
            action('pass'),
        ]

    def decide(self, role_id, system, context, schema):
        stage = context.get('stage_id')
        if stage == 'pretrial_workspace' and context.get('task') in {'choose_action', 'pretrial_action', None}:
            key = (role_id, stage)
            index = self.workflow_turns.get(key, 0)
            self.workflow_turns[key] = index + 1
            self.stats['mock_calls'] += 1
            self.calls.append({'role_id': role_id, 'task': 'pretrial_fixture',
                               'model': 'offline_scripted_mock', 'candidate_label': self.candidate})
            return deepcopy(self.sequence[index]) if index < len(self.sequence) else action('pass')
        if stage == 'between_rounds' and context.get('task') == 'choose_action' and role_id == 'R_DEFENSE' and 'write_artifact' in context.get('allowed_actions', []):
            key = (role_id, 'artifact_update')
            if not self.workflow_turns.get(key):
                self.workflow_turns[key] = 1
                self.stats['mock_calls'] += 1
                self.calls.append({'role_id': role_id, 'task': 'artifact_update_fixture', 'model': 'offline_scripted_mock'})
                return action('write_artifact', target='pretrial_plan.md',
                              text='# 离线准备计划第二版\n\n仅验证第一轮后更新文件；须以实际裁定与调查反馈为准。\n非真实模型结果，非法律标准答案。来源：M01-P001。', references=['M01-P001'])
        return super().decide(role_id, system, context, schema)

    def close(self):
        pass


def create_backend(config, candidate, case):
    return WorkflowMockBackend(case, candidate)
