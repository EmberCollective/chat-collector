"""Service-owned console descriptions; registering a label never loads executable code."""
from copy import deepcopy
import re


SOURCE_KINDS = {'wechat': '微信', 'qq': 'QQ'}
SOURCE_CAPABILITIES = ('query', 'sql', 'collect', 'replay', 'schedule', 'configure', 'files', 'messages')
SOURCE_FIELDS = [
    {'section': 'schedule', 'key': 'refresh_interval_seconds', 'label': '数据库刷新最小间隔（秒，0 为每轮）', 'type': 'number', 'min': 0},
    {'section': 'processing', 'key': 'intensity', 'label': '附件抓取解码强度', 'type': 'select', 'options': {'metadata': '轻量', 'standard': '标准', 'full': '完整'}},
    {'section': 'schedule', 'key': 'initial_lookback_seconds', 'label': '首次回溯时长（秒）', 'type': 'number', 'min': 1, 'max': 2678400},
    *[{'section': 'source', 'key': key, 'label': label, 'type': 'text'} for key, label in (
        ('encrypted_dir', '原始加密数据库目录'), ('decrypted_dir', '明文快照目录'),
        ('work_dir', '临时工作目录（建议放 SSD）'), ('media_dir', '原始媒体目录'),
        ('key_file', '本机密钥文件路径（不显示内容）'), ('resource_db', '资源索引数据库'),
        ('image_cache_dir', '图片缓存目录'))],
    {'section': 'storage', 'key': 'output_dir', 'label': '消息批次输出目录', 'type': 'text'},
    {'section': 'attachments', 'key': 'archive_dir', 'label': '附件归档目录', 'type': 'text'},
    {'section': 'attachments', 'key': 'allowed_roots', 'label': '其他允许读取的目录，每行一个', 'type': 'array'},
    {'section': 'source', 'key': 'refresh', 'label': '正式采集时刷新解密快照', 'type': 'checkbox'},
    {'section': 'attachments', 'key': 'enabled', 'label': '正式采集时保存附件', 'type': 'checkbox'},
]


def source_entries(settings):
    modules, sources = settings.get('modules', {}), settings.get('sources', {})
    if not isinstance(modules, dict) or not isinstance(sources, dict) or set(modules) & set(sources):
        raise ValueError('modules 与 sources 必须是无重复编号的表')
    for identifier, value in {**modules, **sources}.items():
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', identifier) or identifier == 'processor':
            raise ValueError('信息源编号只能包含字母、数字、下划线与连字符，且不能使用 processor')
        if isinstance(value, str):
            value = {'config': value}
        if not isinstance(value, dict) or set(value) - {'kind', 'name', 'config', 'description'}:
            raise ValueError('信息源只支持 kind、name、config、description 描述')
        for key, maximum in (('kind', 100), ('name', 200), ('config', 2000), ('description', 2000)):
            if key in value and (not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > maximum):
                raise ValueError('信息源描述字段无效：' + key)
        if not value.get('kind') and not value.get('config'):
            raise ValueError('信息源需要 kind 或采集配置路径')
        yield identifier, value


def source_descriptor(identifier, spec, config=None):
    kind = spec.get('kind') or config['platform']
    managed = config is not None
    label = SOURCE_KINDS.get(kind, kind)
    return {'id': identifier, 'kind': kind, 'name': spec.get('name', label), 'kind_name': label,
            'description': spec.get('description', '' if managed else '由外部收集器管理，当前控制台未注册此类型的操作'),
            'account_id': config['source']['account_id'] if managed else None,
            'readonly': not managed,
            'capabilities': {key: managed for key in SOURCE_CAPABILITIES},
            'fields': deepcopy(SOURCE_FIELDS) if managed else []}
