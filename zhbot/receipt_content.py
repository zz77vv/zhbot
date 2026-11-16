"""Build customer receipts from the immutable order submission."""
import json


def original_submission(order):
    product = json.loads(order['snapshot'])
    submission = json.loads(order['submission'])
    tiers = json.loads(product.get('tiers', '{}'))
    tier = next((name for name, price in tiers.items() if price == order['price']), None)
    heading = f"✉️ 回单 · {product['name']}"
    if tier:
        heading += f'\n办理档位：{tier}'
    lines = [heading, '', '原报单']
    for index, field in enumerate(product['fields']):
        items = submission.get(str(index), [])
        if not items:
            continue
        if field['kind'] == 'text':
            lines.append(field['name'] + '：' + '\n'.join(item['text'] for item in items))
        else:
            lines.append(field['name'])
            yield from text_parts('\n'.join(lines))
            lines = []
            yield from items
    if lines:
        lines.append('')
    lines.append('回单结果')
    yield from text_parts('\n'.join(lines))


def text_parts(text):
    # Each chunk is a separate, tracked Telegram delivery.
    for start in range(0, len(text), 2000):
        yield {'kind': 'text', 'text': text[start:start + 2000]}


def combined_receipt(context, results):
    if len(results)!=1 or results[0]['kind']!='text' or any(item['kind']!='text' for item in context):
        return None
    text=''.join(item['text'] for item in context)+'\n'+results[0]['text']
    if len(text.encode('utf-16-le'))//2<=2000:
        return {'kind':'text','text':text}
    return None


def bulk_sections(orders, results):
    """Group complete original/result pairs within the existing message budget."""
    groups = []
    group = []
    length = 0
    for index, (account, order) in enumerate(orders.items(), 1):
        result = results[account]
        display = f"空单，退回 {order['price']} 积分" if result == '空' else result
        text = f'{index}. 原报单：{account}\n回单：{display}'
        size = len(text.encode('utf-16-le')) // 2 + 2
        if size > 3600:
            return None
        if group and length + size > 3600:
            groups.append(group)
            group, length = [], 0
        group.append((order, result, text))
        length += size
    if group:
        groups.append(group)
    return groups
