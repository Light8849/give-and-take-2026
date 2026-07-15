#!/usr/bin/env python3
"""ゼロから時間割自動編成の実証実験.

入力（v23の解そのものは使わない。使うのは条件だけ）:
- コマ定義: 科目グループ（クラス集合・教員集合・週コマ数・連続要件）
- クラスの許可枠（基準=コマ数のため使用枠と同一）
- 教員の×グリッド・会議（師楽CSV由来）
- 教室割当と容量、雨天ルール、昼ルール、1日/連続上限（v23実績プロキシ）

配置（どのコマをどの枠に置くか）はすべてソルバーが決める。
"""
import json, re, collections, sys, time
from ortools.sat.python import cp_model

html = open('/home/user/give-and-take-2026/index.html', encoding='utf-8').read()
m = re.search(r'<script[^>]*id="jdata"[^>]*>(.*?)</script>', html, re.S)
jd = json.loads(m.group(1))
conv = json.load(open('/tmp/claude-0/-home-user-give-and-take-2026/3d193d35-3740-551b-80ca-bea354675ec2/scratchpad/converted.json'))

# ===== 入力条件の構築 =====
G = {}
for e in jd['entries']:
    g = G.setdefault(e['pcode'], {'lesson': e['lesson'], 'subject': e['subject'],
                                  'classes': frozenset(e['all_classes']),
                                  'teachers': frozenset(e['teachers']), 'slots': set()})
    g['slots'].add((e['day'], e['period']))

class_grid = collections.defaultdict(set)
for e in jd['entries']:
    class_grid[e['class_name']].add((e['day'], e['period']))

blocked = {t: set(tuple(map(int, x.split('-'))) for x in v) for t, v in conv['teacher_blocked'].items()}
meetings = {t: set(tuple(map(int, x.split('-'))) for x in v) for t, v in conv['teacher_meetings'].items()}

conv_idx = {(e['class_name'], e['day'], e['period']): e['room'] for e in conv['entries'] if e['room']}
COMPOSITE = {'高/高/高': ['高校音楽', '高校書道', '高校美術'], '中/中': ['中学音楽', '中学美術']}
rooms_of = {}
for pc, g in G.items():
    rooms = {conv_idx.get((c, d, p)) for c in g['classes'] for (d, p) in g['slots']}
    rooms.discard(None)
    if len(rooms) == 1:
        r = rooms.pop()
        rooms_of[pc] = COMPOSITE.get(r, [r])   # 複合教室は構成室に分解
ROOM_CAP = collections.defaultdict(lambda: 1)
ROOM_CAP['外／中'] = 4

def is_rain(g):
    L = g['lesson']
    return g['subject'] == '保健体育' and not L.startswith('保健') and '教室' not in L \
        and '柔道' not in L and '弓道' not in L

# 連続要件: v23で全スロットが同日隣接の複数コマ授業 → 連続ブロック設計とみなす
def consec_req(g):
    ss = sorted(g['slots'])
    if len(ss) < 2: return 0
    days = {d for d, _ in ss}
    if len(days) != 1: return 0
    ps = sorted(p for _, p in ss)
    if all(b == a + 1 for a, b in zip(ps, ps[1:])):
        return len(ps)
    return 0

# 上限プロキシ（v23実績と全体値の大きい方）
def streaks(ps):
    ps = sorted(ps); best = cur = 1
    for a, b in zip(ps, ps[1:]):
        cur = cur + 1 if b == a + 1 else 1; best = max(best, cur)
    return best
busy0 = collections.defaultdict(dict)
for pc, g in G.items():
    for s in g['slots']:
        for t in g['teachers']: busy0[t][s] = pc
t_daily, t_consec = {}, {}
for t, mp in busy0.items():
    byd = collections.defaultdict(list)
    for (d, p) in mp: byd[d].append(p)
    t_daily[t] = max(max((len(v) for v in byd.values()), default=0), 5)
    t_consec[t] = max(max((streaks(v) for v in byd.values()), default=1), 4)

def is_mid(c): return c.startswith('中学')

ALL_TEACHERS = set()
for g in G.values(): ALL_TEACHERS |= g['teachers']

# 候補枠: クラス全部の許可枠 ∩ 教員全員の非×・非会議枠 ＋ 昼帯の学校ルール
def candidates(g):
    cand = None
    for c in g['classes']:
        cand = set(class_grid[c]) if cand is None else cand & class_grid[c]
    out = set()
    for (d, p) in cand:
        if any(is_mid(c) and p == 5 or (not is_mid(c)) and p == 4 for c in g['classes']):
            continue
        ok = True
        for t in g['teachers']:
            if (d, p) in blocked.get(t, set()) or (d, p) in meetings.get(t, set()):
                ok = False; break
        if ok: out.add((d, p))
    return out

model = cp_model.CpModel()
X = {}          # (pc, slot) -> BoolVar
cand_of = {}
infeasible = []
for pc, g in G.items():
    cand = candidates(g)
    cr = consec_req(g)
    cand_of[pc] = cand
    if len(cand) < len(g['slots']):
        infeasible.append((pc, g['lesson'], len(cand), len(g['slots'])))
    for s in cand:
        X[(pc, s)] = model.NewBoolVar(f'x_{pc}_{s[0]}_{s[1]}')

print('科目グループ:', len(G), '/ 候補不足:', infeasible[:5] if infeasible else 'なし')

# 各グループ: 週コマ数ぶん配置
for pc, g in G.items():
    model.Add(sum(X[(pc, s)] for s in cand_of[pc]) == len(g['slots']))

# 連続ブロック: 連続コマは同日隣接ペア（トリプル）で置く → ブロック開始変数で表現
for pc, g in G.items():
    cr = consec_req(g)
    if cr >= 2:
        starts = []
        byday = collections.defaultdict(list)
        for (d, p) in cand_of[pc]: byday[d].append(p)
        block_lits = []
        for d, ps in byday.items():
            pset = set(ps)
            for p in sorted(ps):
                if all(p + k in pset for k in range(cr)):
                    b = model.NewBoolVar(f'blk_{pc}_{d}_{p}')
                    block_lits.append((b, d, p))
        model.Add(sum(b for b, _, _ in block_lits) == 1)
        for s in cand_of[pc]:
            covers = [b for b, d, p in block_lits if d == s[0] and p <= s[1] < p + cr]
            if covers:
                model.Add(X[(pc, s)] == sum(covers))
            else:
                model.Add(X[(pc, s)] == 0)

# クラスの完全被覆（各許可枠にちょうど1つ）
for c, grid in class_grid.items():
    for s in grid:
        covering = [X[(pc, s)] for pc, g in G.items() if c in g['classes'] and s in cand_of[pc]]
        model.Add(sum(covering) == 1)

# 教員: 同時1、1日上限、連続上限、昼（4/5どちらか）
teacher_slot = {}
for t in ALL_TEACHERS:
    lessons = [pc for pc, g in G.items() if t in g['teachers']]
    for d in range(1, 7):
        for p in range(1, 10):
            xs = [X[(pc, (d, p))] for pc in lessons if (d, p) in cand_of[pc]]
            if xs:
                y = model.NewBoolVar(f'y_{t}_{d}_{p}')
                model.Add(sum(xs) <= 1)
                model.AddMaxEquality(y, xs)
                teacher_slot[(t, d, p)] = y
    for d in range(1, 7):
        ys = [teacher_slot[(t, d, p)] for p in range(1, 10) if (t, d, p) in teacher_slot]
        if not ys: continue
        model.Add(sum(ys) <= t_daily.get(t, 5))
        y4, y5 = teacher_slot.get((t, d, 4)), teacher_slot.get((t, d, 5))
        if y4 is not None and y5 is not None:
            model.Add(y4 + y5 <= 1)
        L = t_consec.get(t, 4)
        ps = [p for p in range(1, 10) if (t, d, p) in teacher_slot]
        for i in range(len(ps)):
            win = [q for q in ps[i:] if q - ps[i] <= L and all(r in ps for r in range(ps[i], q + 1))]
            seq = [teacher_slot[(t, d, q)] for q in range(ps[i], ps[i] + L + 1) if (t, d, q) in teacher_slot]
            if len(seq) == L + 1:
                model.Add(sum(seq) <= L)

# 教室容量・雨天ルール
slots_all = set()
for g in G.values(): slots_all |= set(class_grid[next(iter(g['classes']))])
for c, grid in class_grid.items(): slots_all |= grid
for s in slots_all:
    byroom = collections.defaultdict(list)
    rains = []
    for pc, g in G.items():
        if s in cand_of[pc]:
            for r in rooms_of.get(pc, []):
                byroom[r].append(X[(pc, s)])
            if is_rain(g): rains.append(X[(pc, s)])
    for r, xs in byroom.items():
        model.Add(sum(xs) <= ROOM_CAP[r])
    if rains:
        model.Add(sum(rains) <= 2)      # 雨天理想ルール

# 品質目標: 複数コマ科目（非連続設計）は同じ日に2回入れない → ソフト制約
penalties = []
for pc, g in G.items():
    if len(g['slots']) >= 2 and consec_req(g) == 0:
        byday = collections.defaultdict(list)
        for (d, p) in cand_of[pc]: byday[d].append(p)
        for d, ps in byday.items():
            if len(ps) >= 2:
                pen = model.NewIntVar(0, len(ps), f'pen_{pc}_{d}')
                model.Add(pen >= sum(X[(pc, (d, p))] for p in ps) - 1)
                penalties.append(pen)
model.Minimize(sum(penalties))

print('変数:', len(X), '/ ソフト目標(同日重複ペナルティ)項:', len(penalties))
solver = cp_model.CpSolver()
solver.parameters.max_time_in_seconds = 240
solver.parameters.num_workers = 8
t0 = time.time()
status = solver.Solve(model)
el = time.time() - t0
print('status:', solver.StatusName(status), '| %.1f秒' % el)
if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
    print('同日重複ペナルティ合計:', int(solver.ObjectiveValue()), '（v23実測: 131件相当・うち非連続7）')
    sol = {}
    for (pc, s), v in X.items():
        if solver.Value(v):
            sol.setdefault(pc, []).append(s)
    out = []
    for pc, ss in sol.items():
        g = G[pc]
        for s in ss:
            for c in g['classes']:
                out.append({'pcode': pc, 'day': s[0], 'period': s[1], 'class_name': c,
                            'lesson': g['lesson'], 'subject': g['subject'],
                            'teachers': sorted(g['teachers']), 'all_classes': sorted(g['classes']),
                            'is_deploy': len(g['teachers']) > 1})
    json.dump({'entries': out, 'teachers': jd['teachers'],
               'teacher_blocked': conv['teacher_blocked'],
               'teacher_meetings': conv['teacher_meetings'],
               'teacher_off_days': {},
               'title': 'AI自動編成（ゼロから・実証実験）'},
              open('/tmp/claude-0/-home-user-give-and-take-2026/3d193d35-3740-551b-80ca-bea354675ec2/scratchpad/ai_timetable.json', 'w'),
              ensure_ascii=False)
    print('解を ai_timetable.json に保存')
