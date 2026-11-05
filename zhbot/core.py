"""Shared, deliberately bounded input validation."""


class RuleError(ValueError):
    """A safe, user-readable business rejection."""


MAX_POINTS = 1_000_000
MAX_BALANCE = 1_000_000_000_000
MAX_ID = 9_223_372_036_854_775_807
MAX_ITEMS = 20


def parse_uint(value, *, maximum=MAX_ID, minimum=1):
    if type(value) is int:
        result = value
    elif (type(value) is str and 0 < len(value) <= len(str(maximum))
          and value.isascii() and value.isdecimal()):
        result = int(value)
    else:
        raise RuleError('请输入有效的 ASCII 整数。')
    if not minimum <= result <= maximum:
        raise RuleError(f'整数范围为 {minimum}～{maximum}。')
    return result


def bounded_text(value, maximum=3500):
    if type(value) is not str or not value.strip():
        raise RuleError('内容不能为空。')
    try:
        length = len(value.encode('utf-16-le')) // 2
    except UnicodeError:
        raise RuleError('内容编码无效。') from None
    if length > maximum:
        raise RuleError(f'内容过长，最多 {maximum} 个 UTF-16 单位。')
    return value


def item_values(kind, content, caption=''):
    if kind not in {'text', 'photo', 'document'}:
        raise RuleError('仅支持文字、图片和文件。')
    bounded_text(content, 3500 if kind == 'text' else 512)
    if caption:
        bounded_text(caption, 1000)
    return kind, content, caption
