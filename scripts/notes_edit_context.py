"""Read-only semantic tool observations. No inferred pixel/icon positions."""

TOOLS = {'ballPointButton':'pen','ballPointPenButton':'pen','brushPencilButton':'pen',
         'markpenButton':'pen','brushMarkButton':'pen','eraserButton':'eraser',
         'lassoButton':'lasso','oneStrokeButton':'shape','laserButton':'laser',
         'textButton':'text','imageButton':'image','stickerButton':'sticker'}
ACTIONS = {'FirstToolBarUndoButton':'undo','FirstToolBarRedoButton':'redo'}
MODE_LABELS = {'笔画擦除':'stroke','笔划擦除':'stroke','整笔擦除':'stroke','点擦除':'point',
               '局部擦除':'point','圈选擦除':'region','Stroke eraser':'stroke','Point eraser':'point'}
MODE_IDS = {'pointEraserButton':'point','lineEraserButton':'stroke','lassoEraserButton':'region'}
LASSO_ACTIONS = {'剪切':'cut','复制':'copy','删除':'delete','调整大小':'resize',
                 '颜色':'color','转换':'convert','字迹调整':'adjust_handwriting'}


def summarize_edit_tools(tree):
    from notes_diagnostics import nodes,bounds
    controls=[];selected=set();modes=set();mode_choices=[];highlights=set();lasso_actions=set()
    for node,parents in nodes(tree):
        identity=node.get('compid','');base=identity.removesuffix('Selected')
        attrs=node.get('$attrs',{})
        # Real device survey: the blue Row moves from lineEraserButton to
        # pointEraserButton and back. Treat this style as a candidate, not an
        # explicit selected attribute or persistent mode after menu dismissal.
        owner=next((p.get('compid') for p in reversed((*parents,node))
                    if p.get('compid') in MODE_IDS),None)
        if owner and node.get('$type')=='Row' and node.get('backgroundColor')=='#FF0A59F7':
            highlights.add(MODE_IDS[owner])
        if any(p.get('compid')=='lassoPopWindow' for p in parents):
            if attrs.get('content') in LASSO_ACTIONS:lasso_actions.add(LASSO_ACTIONS[attrs['content']])
        if base in TOOLS or identity in ACTIONS:
            semantic=TOOLS.get(base) or ACTIONS[identity]
            chosen=identity.endswith('Selected') or attrs.get('selected') in (True,'true')
            if chosen and base in TOOLS:selected.add(semantic)
            control={'id':identity,'kind':semantic,'selected':chosen,'node_id':node.get('$ID'),
                     'window_bounds':bounds(node.get('$rect'))}
            if control['window_bounds'] and control not in controls:controls.append(control)
        text=attrs.get('content')
        if text in MODE_LABELS:
            # A visible menu option is not the current mode. Only explicit
            # selection attributes on its own node or direct parent qualify.
            marked=any(p.get('$attrs',{}).get('selected') in (True,'true') or
                       p.get('$attrs',{}).get('checked') in (True,'true')
                       for p in (node,*(parents[-1:])))
            mode_choices.append({'label':text,'mode':MODE_LABELS[text],'explicit_selected':marked})
            if marked:modes.add(MODE_LABELS[text])
    return {'schema':1,'selected_tool':next(iter(selected)) if len(selected)==1 else None,
            'selected_tool_candidates':sorted(selected),
            'eraser_mode':next(iter(modes)) if len(modes)==1 else None,
            'eraser_mode_style_candidate':next(iter(highlights)) if len(highlights)==1 and
                (not modes or modes==highlights) else None,
            'eraser_mode_style_evidence':'observed_20261017_mode_button_blue_row' if highlights else None,
            'lasso_menu_available_actions':sorted(lasso_actions),
            'lasso_menu_actions_executed':False,
            'eraser_mode_choices':mode_choices[:12],'controls':controls[:48],
            'evidence':'visible_notes_component_ids_and_explicit_selection',
            'native_edit_result_verified':False,'window_bounds_are_screen_coordinates':False}


def geometry_context(context):
    """Tool observations must not invalidate geometry solely due to UI metadata."""
    return {k:v for k,v in (context or {}).items() if k!='editing'}
