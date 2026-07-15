#!/usr/bin/env python3
"""師楽の時間割CSV（表示画面のCSV書出）→ Give&Take互換JSON 変換ツール.

入力（いずれも cp932。表示メニューのCSVボタンで書出したもの）:
  --zentai  全体時間割.CSV   … クラス×コマの 科目/先生/教室 3行組
  --sensei  全先生時間割.CSV … 教員×コマの 科目/クラス 2行組（×=不可時限、会議名も出る）
  --master  科目名称.csv     … 名称マスタ（切り詰められた科目名の復元用・任意）

出力: jdata互換JSON（entries に room 付き）＋検証レポート（stderr）

制約: CSVは表示幅で名称が切り詰められる。科目名は名称マスタへの前方一致で復元を試み、
曖昧なものは candidates として残す。教員名は全先生時間割の行名（完全名）から結合するため正確。
"""
import argparse, csv, io, json, sys, collections

DAYS = {'月':1,'火':2,'水':3,'木':4,'金':5,'土':6}
ZDIG = {'１':1,'２':2,'３':3,'４':4,'５':5,'６':6,'７':7,'８':8,'９':9}


def read_rows(path):
    with open(path, 'rb') as f:
        txt = f.read().decode('cp932')
    return [row for row in csv.reader(io.StringIO(txt))]


def col_slots(rows):
    """ヘッダ2行（曜日行・時限行）→ 列index→(day,period)."""
    dayrow, prow = rows[0], rows[1]
    day = None
    mapping = {}
    for i in range(1, max(len(dayrow), len(prow))):
        d = dayrow[i].strip() if i < len(dayrow) else ''
        if d in DAYS:
            day = DAYS[d]
        p = prow[i].strip() if i < len(prow) else ''
        if day and p in ZDIG:
            mapping[i] = (day, ZDIG[p])
    return mapping


def clean(s):
    return s.strip().replace('　', '')


def parse_zentai(rows):
    """→ {(class,day,period): {'lesson':str,'teacher_disp':str,'room':str}}"""
    m = col_slots(rows)
    out = {}
    i = 2
    while i + 2 < len(rows) + 1 and i < len(rows):
        name_row = rows[i]
        cls = clean(name_row[0])
        if not cls:
            i += 1
            continue
        t_row = rows[i+1] if i+1 < len(rows) else []
        r_row = rows[i+2] if i+2 < len(rows) else []
        for col, slot in m.items():
            lesson = clean(name_row[col]) if col < len(name_row) else ''
            if not lesson or lesson.startswith('×'):
                continue
            t = clean(t_row[col]) if col < len(t_row) else ''
            r = clean(r_row[col]) if col < len(r_row) else ''
            if t == '×':
                continue
            out[(cls, slot[0], slot[1])] = {
                'lesson': lesson,
                'teacher_disp': t,
                'room': '' if r in ('', '＊') else r,
            }
        i += 3
    return out


def parse_sensei(rows):
    """→ teachers[name] = {'grid': {(d,p): cellinfo}, 'blocked': set, 'meetings': {(d,p): name}}
    cellinfo: クラス名 or ''（授業行は科目のみのため INFO 行のクラス名で判定）"""
    m = col_slots(rows)
    teachers = {}
    i = 2
    while i + 1 < len(rows):
        name = clean(rows[i][0])
        if not name:
            i += 1
            continue
        info = rows[i+1] if i+1 < len(rows) else []
        g, blocked, meet = {}, set(), {}
        for col, slot in m.items():
            v = clean(info[col]) if col < len(info) else ''
            subj = clean(rows[i][col]) if col < len(rows[i]) else ''
            if v == '×':
                blocked.add(slot)
            elif v and (v.startswith('/') or set(v) == {'/'}):
                g[slot] = ('LHR', subj)
            elif v and not any(c.isdigit() or c == '-' for c in v) and '中学' not in v and subj == '':
                meet[slot] = v          # 会議名（授業行が空でINFO行に名称）
            elif v:
                g[slot] = (v, subj)     # クラス名（授業あり）
        teachers[name] = {'grid': g, 'blocked': blocked, 'meetings': meet}
        i += 2
    return teachers


def resolve_lesson(trunc, master, cache):
    if not master or trunc in master:
        return trunc, []
    if trunc in cache:
        return cache[trunc]
    cands = [x for x in master if x.startswith(trunc)]
    res = (cands[0], cands) if len(cands) == 1 else (trunc, cands)
    cache[trunc] = res
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--zentai', required=True)
    ap.add_argument('--sensei', required=True)
    ap.add_argument('--master')
    ap.add_argument('--rooms', help='教室情報.csv（教室名マスタ。切り詰め名の照合用）')
    ap.add_argument('-o', '--out', default='converted.json')
    a = ap.parse_args()

    zen = parse_zentai(read_rows(a.zentai))
    sen = parse_sensei(read_rows(a.sensei))
    def load_list(path):
        if not path:
            return []
        raw = open(path, 'rb').read()
        try:
            txt = raw.decode('utf-8')
        except UnicodeDecodeError:
            txt = raw.decode('cp932')
        return [clean(l) for l in txt.splitlines() if clean(l)]

    master = load_list(a.master)
    rooms_master = load_list(a.rooms)

    # (class,slot) → 担当教員（完全名）を全先生時間割から結合
    slot_teachers = collections.defaultdict(list)
    for tname, td in sen.items():
        for slot, (cls, _subj) in td['grid'].items():
            if cls == 'LHR':
                continue
            slot_teachers[(cls, slot[0], slot[1])].append(tname)

    # 合併授業（同一時限・同一科目名・複数クラス）は教員をグループ全体で共有する
    group = collections.defaultdict(list)   # (day,p,lesson) -> [class,...]
    for (cls, day, p), v in zen.items():
        group[(day, p, v['lesson'])].append(cls)
    group_teachers = {}
    for key, clss in group.items():
        ts = set()
        for c in clss:
            ts.update(slot_teachers.get((c, key[0], key[1]), []))
        group_teachers[key] = sorted(ts)

    cache, ambiguous = {}, collections.Counter()
    entries = []
    for (cls, day, p), v in sorted(zen.items()):
        lesson, cands = resolve_lesson(v['lesson'], master, cache)
        if len(cands) > 1:
            ambiguous[v['lesson']] += 1
        ts = slot_teachers.get((cls, day, p), []) or group_teachers.get((day, p, v['lesson']), [])
        room = v['room'].replace(' ', '')
        if rooms_master:
            cand = [r for r in rooms_master if r.replace(' ', '').startswith(room.rstrip('/')) and room]
            room = cand[0] if len(cand) == 1 and room else ('' if set(room) <= {'/'} else room)
        entries.append({'day': day, 'period': p, 'class_name': cls, 'lesson': lesson,
                        'teachers': sorted(ts), 'room': room,
                        'teacher_disp': v['teacher_disp'],
                        'all_classes': sorted(group[(day, p, v['lesson'])])})

    # 学校レベルで閉鎖されている土4〜9限は個人の×から除外（全員に付くため）
    SCHOOL_CLOSED = {(6, p) for p in range(4, 10)}
    out = {
        'entries': entries,
        'teacher_blocked': {t: sorted(f'{d}-{p}' for d, p in td['blocked'] - SCHOOL_CLOSED)
                            for t, td in sen.items()},
        'teacher_meetings': {t: sorted(f'{d}-{p}' for d, p in td['meetings']) for t, td in sen.items()},
    }
    with open(a.out, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print(f'entries: {len(entries)} / teachers: {len(sen)}', file=sys.stderr)
    print(f'教室付きコマ: {sum(1 for e in entries if e["room"])}', file=sys.stderr)
    noteacher = [e for e in entries if not e['teachers']]
    print(f'教員を結合できなかったコマ: {len(noteacher)}', file=sys.stderr)
    if ambiguous:
        print(f'科目名が曖昧（前方一致複数）: {dict(ambiguous.most_common(10))}', file=sys.stderr)


if __name__ == '__main__':
    main()
