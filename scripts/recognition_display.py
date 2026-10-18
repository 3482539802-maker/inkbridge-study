"""Escaped multi-region recognition display shared by tutoring and knowledge pages."""
from html import escape

LABELS = {'formula': '公式', 'formula_fragment': '公式片段', 'text': '文字',
          'diagram': '图形', 'annotation': '标注', 'mixed': '混合内容', 'unknown': '未知'}


def recognition_html(groups):
    cards = []
    for index, group in enumerate(groups, 1):
        pieces = []
        for element in group.get('elements', []):
            pieces.append('<p><strong>'+escape(LABELS.get(element.get('kind'), '未知'))+'</strong> · '
                +escape(element.get('text', ''))+'</p>')
            if element.get('latex'): pieces.append('<pre>'+escape(element['latex'])+'</pre>')
            if element.get('uncertainties'):
                pieces.append('<p class="muted">不确定：'+escape('；'.join(element['uncertainties']))+'</p>')
        if not pieces:
            pieces.append('<p>'+escape(group.get('latex_candidate') or group.get('reason') or group.get('status', '未知'))+'</p>')
        if group.get('uncertainties'):
            pieces.append('<p class="muted">'+escape('；'.join(group['uncertainties']))+'</p>')
        cards.append('<div class="recognition-group"><h4>片段 '+str(index)+' · '
            +escape(LABELS.get(group.get('semantic_type'), '候选'))+'</h4>'+''.join(pieces)+'</div>')
    return ''.join(cards)
