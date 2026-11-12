"""Set filters.code_file to this file, then use type='text_length' in rules."""
from collector.filters import fields


def register_filters(registry):
    def text_length(rule, compiler):
        fields(rule, ('minimum',))
        minimum = rule['minimum']
        if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
            raise ValueError('text_length.minimum must be a nonnegative integer')
        return 'length(text) >= '+compiler.bind(minimum)
    registry.register('text_length', text_length)
