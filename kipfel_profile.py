"""Kipfel 战术版（创意工坊 3558183296）的 mod 描述：文件位置、装饰部件表、预设。

这里只有「数据」，换一个 mod 时主要改这个文件；读写、校验、界面逻辑都在 wardrobe.py。
所有内容都由作者的模型逐件核对过（见 README「如何核对部件」）。

部件（装饰）定义：每件装饰用「选择规则」认领模型里几何上独立的小件：
  mat  材质名（小写）
  bp   部件组（bodygroup）名
  x/y/z  小件包围盒中心点的坐标范围 (最小, 最大)，None 表示不限
         Source 坐标：y 负 = 正前方，z = 高度；角色自己的右侧是 x 负方向，容易搞反，所以名称里不写左右。
写了 bp 或坐标的规则优先于只写材质的规则，每个小件只归一件装饰；都不匹配的小件不受控制（例如身体、内衣）。
一个材质的小件全部被隐藏 → 改材质（VMT）；只隐藏其中一部分 → 在本地版 .vtx 里把这些小件的三角形改成退化三角形。
  hide        是否可以隐藏（基础服装只能染色）
  parent      父级被隐藏时它也跟着隐藏（例如帽子上的蝴蝶结）
  tint        染色作用的材质（整个材质一起变）
  when_hidden 隐藏时额外显示 / 隐藏的材质（例如摘帽子露出头顶的猫耳头发）
"""

WORKSHOP_ID = '3558183296'
WORKSHOP_URL = 'https://steamcommunity.com/sharedfiles/filedetails/?id=' + WORKSHOP_ID
MOD_NAME = 'Kipfel丨可爱猫猫丨夜光丨For Louis'
MOD_AUTHOR = 'Mr.limbo'
CHARACTER = 'Kipfel'
SURVIVOR = 'Louis'

MAT_DIR = 'materials/kipfelzhanshu/kipfelzhanshu/'     # VPK 里的材质目录（小写）
MAT_REF = 'Kipfelzhanshu\\Kipfelzhanshu/'               # VMT 里引用贴图时用的前缀
HIDDEN_TEXTURE = MAT_REF + 'alpha'                      # mod 自带的全透明贴图
MODELS = {                                              # 预览用的模型（第三人称 / 第一人称手臂）
    'survivor': 'models/survivors/survivor_manager',
    'arms': 'models/weapons/arms/v_arms_louis',
}
BASE_HIDDEN = ['hairxh', 'bodybase']                    # 原版就隐藏着的材质
VISIBLE_TEMPLATE = {'hairxh': 'hair', 'bodybase': 'hedgemazebodybase'}  # 显示它们时借用的材质

GROUPS = [
    dict(id='gear', name='战术装备'),
    dict(id='head', name='头部配饰'),
    dict(id='body', name='身上配饰'),
    dict(id='wear', name='服装与发色', note='基础服装只能染色，不能脱下'),
]


def _sel(mat, bp=None, **box):
    return dict(mat=mat, bp=bp, **box)


PARTS = [
    # —— 战术装备
    dict(id='gloves', group='gear', name='手套', desc='CS2 运动手套「树篱迷宫」，第一 / 第三人称同步',
         sel=[_sel('hedgemaze')], hide=True, tint=['hedgemaze'],
         when_hidden=dict(show=['bodybase'], hide=['hedgemazebodybase'])),
    dict(id='goggles', group='gear', name='护目镜', desc='镜框、镜片、头带和脑后的金属调节扣',
         sel=[_sel('sv_parts2'), _sel('glass'), _sel('silverdgz', 'outfit1')], hide=True),
    dict(id='plate', group='gear', name='战术背心', desc='背心主体和胸前插扣',
         sel=[_sel('tac 2')], hide=True),
    dict(id='backpatch', group='gear', name='背后的 LimboFactory 贴片', parent='plate',
         sel=[_sel('tac 2', y=(7.5, None), z=(44, 50))], hide=True),
    dict(id='flashlight', group='gear', name='胸前战术手电', parent='plate',
         sel=[_sel('tac 2', x=(-9.5, -6), y=(-10, -6.4), z=(37, 45))], hide=True),
    dict(id='sidepouch', group='gear', name='腰两侧挂包', desc='一侧是挂包，另一侧是双联小包', parent='plate',
         sel=[_sel('tac 2', x=(None, -6.7), z=(31.5, 38.5)), _sel('tac 2', x=(6.5, None), z=(32.5, 38))], hide=True),
    dict(id='rig', group='gear', name='弹匣包与战术腰带', desc='胸前弹匣包、腰带和挂带',
         sel=[_sel('tac 1')], hide=True),
    dict(id='glowsticks', group='gear', name='腰间荧光棒', desc='挂在腰侧的几根绿色荧光棒',
         sel=[_sel('tac 1', x=(6.4, 10), y=(-5.6, -0.95), z=(38, 44.5))], hide=True),
    dict(id='tassel', group='gear', name='绿色流苏', desc='腰侧垂下的两股绿色流苏',
         sel=[_sel('tac 1', x=(5.4, 8.8), y=(-9.8, -7.3), z=(29, 36.2))], hide=True),
    dict(id='thighrig', group='gear', name='大腿枪套', desc='大腿上的枪套和绑带',
         sel=[_sel('tac 1', z=(None, 28.6)), _sel('tac 2', z=(None, 28))], hide=True),
    # —— 头部配饰
    dict(id='hat', group='head', name='猫耳帽', desc='摘下后露出头发和毛茸茸的猫耳',
         sel=[_sel('outfit02dgz')], hide=True, when_hidden=dict(show=['hairxh'])),
    dict(id='hatbow', group='head', name='帽子后的大蝴蝶结', desc='含两条长飘带', parent='hat',
         sel=[_sel('outfit02dgz', y=(7.5, None))], hide=True),
    dict(id='fishbone', group='head', name='鱼骨别针', desc='别在帽子侧面', parent='hat',
         sel=[_sel('silverdgz', 'item2', z=(50, None))], hide=True),
    dict(id='glasses', group='head', name='圆框眼镜', desc='黑框圆眼镜（含镜片）',
         sel=[_sel('outfit02xh'), _sel('outfit_alphaxh')], hide=True),
    dict(id='hairbow', group='head', name='侧边黑色蝴蝶结', desc='系在侧边头发上的长飘带蝴蝶结',
         sel=[_sel('outfit02', 'hair2')], hide=True),
    dict(id='clips', group='head', name='发夹', desc='刘海上的两枚发夹',
         sel=[_sel('silver')], hide=True),
    dict(id='hairtie', group='head', name='发尾发圈', desc='长发末端的黑色发圈',
         sel=[_sel('outfit02', 'hair')], hide=True),
    # —— 身上配饰
    dict(id='tie', group='body', name='领带', desc='含领结',
         sel=[_sel('outfit01xh', 'outfit1', x=(-1.6, 1.6), y=(-7.5, -3.25), z=(34, 52.5))], hide=True),
    dict(id='bottlecap', group='body', name='Limbo 瓶盖挂牌', desc='腰间挂着的瓶盖吊牌',
         sel=[_sel('silverdgz', 'item2', z=(None, 50))], hide=True),
    dict(id='card', group='body', name='透明卡套', desc='胸前挂着的透明卡套',
         sel=[_sel('outfit_alphadgz')], hide=True),
    dict(id='bag', group='body', name='斜挎包', desc='迷彩背带的小挎包',
         sel=[_sel('outfit01dgz', 'item', z=(22, None))], hide=True),
    dict(id='bandage', group='body', name='大腿创可贴', desc='大腿上的白色十字贴',
         sel=[_sel('outfit02', 'item')], hide=True),
    dict(id='boots', group='body', name='靴子', desc='含鞋带和金属扣眼，脱下后露出袜子',
         sel=[_sel('outfit01dgz', 'item', z=(None, 22)), _sel('silverdgz', 'item')], hide=True),
    # —— 服装与发色（只能染色）
    dict(id='shirt', group='wear', name='衬衫', desc='染色会连同领带、第一人称袖子一起变',
         sel=[_sel('outfit01xh')], hide=False, tint=['outfit01xh']),
    dict(id='knit', group='wear', name='针织背心', desc='和斜挎包、靴子共用材质，染色会一起变',
         sel=[_sel('outfit01dgz', 'outfit2')], hide=False, tint=['outfit01dgz']),
    dict(id='skirt', group='wear', name='短裙与袜子', desc='共用一个材质',
         sel=[_sel('outfit01')], hide=False, tint=['outfit01']),
    dict(id='hair', group='wear', name='发色', desc='包括猫耳和眉毛',
         sel=[_sel('hair'), _sel('hairxh')], hide=False, tint=['hair', 'hairxh']),
]

# 旧版（v1/v2）配置里的粗粒度部件 id 换算到现在的细分部件
LEGACY_SPLITS = {
    'plate': ['plate', 'backpatch', 'flashlight', 'sidepouch', 'thighrig'],
    'rig': ['rig', 'glowsticks', 'tassel', 'thighrig'],
    'metal': ['fishbone', 'bottlecap'],
}

_GEAR = [p['id'] for p in PARTS if p['group'] == 'gear']
PRESETS = [  # (名称, 要隐藏的部件)
    ('原版', []),
    ('去掉手套', ['gloves']),
    ('卸下战术装备', _GEAR),
    ('便装 · 不戴帽', _GEAR + ['hat']),
    ('清爽 · 只留衣服', [p['id'] for p in PARTS if p['hide'] and p['id'] != 'boots']),
]
