#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把订单级的优惠与运费分摊到商品行，产出 SQLite 行级事实表与逐单勾稽结果。

输入
    orders.csv       订单主表（一单一行，金额单位：分）
    order_items.csv  商品子表（一行一个商品行）

输出
    order_fact.db             SQLite，表 order_line_fact
    reconciliation.csv        逐单勾稽（合计与差额）
    duplicate_orders.csv      主表重复单的处理明细
    excluded_orders.csv       未进事实表的单及原因

口径
    行原额 = 数量 × 行单价
    应摊合计 = 商品合计 - 优惠 + 运费        （题面口径，也是本次分摊的目标总额）
    行净额 = 行原额 - 行优惠分摊 + 行运费分摊
    优惠、运费各自按行原额比例分摊，最大余额法取整，保证分摊额逐单精确合计。

主表处理
    1) 同一订单号出现多行的，视为上游重跑抽取叠加的快照，按文件顺序保留最后一行
       （最后一行是更晚的快照）；被丢弃的行写入 duplicate_orders.csv。
    2) 没有支付时间的单视为未成交，不进事实表，写入 excluded_orders.csv。
    3) 应付总额与「商品合计-优惠+运费」对不平的单照常进表，分摊目标仍取后者，
       与应付总额的差额在 reconciliation.csv 的「应付总额差」列中如实暴露。

重跑：所有输出整表重建，内容与上一次完全一致（无时间戳、无随机数、排序固定）。
"""

import argparse
import csv
import os
import sqlite3
import sys
from collections import OrderedDict

ORDER_COLS = ["订单号", "下单时间", "支付时间", "应付总额", "运费", "优惠"]
ITEM_COLS = ["订单号", "商品号", "数量", "行单价"]


class DataError(Exception):
    pass


def read_rows(path, required):
    if not os.path.isfile(path):
        raise DataError("找不到文件: %s" % path)
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in required if c not in (reader.fieldnames or [])]
        if missing:
            raise DataError("%s 缺少列: %s" % (path, ",".join(missing)))
        rows = []
        for line_no, row in enumerate(reader, start=2):
            # 整行全空（文件尾多余换行等）跳过
            if all((v or "").strip() == "" for v in row.values() if v is not None):
                continue
            row["_line"] = line_no
            rows.append(row)
    if not rows:
        raise DataError("%s 没有数据行" % path)
    return rows


def parse_int(value, field, row):
    text = (value or "").strip()
    if text == "":
        raise DataError("第 %s 行 %s 为空" % (row["_line"], field))
    try:
        return int(text)
    except ValueError:
        raise DataError("第 %s 行 %s 不是整数: %r" % (row["_line"], field, value))


def load_orders(path):
    """读取主表并按订单号去重：同号多行保留文件中最后出现的一行。"""
    rows = read_rows(path, ORDER_COLS)
    kept = OrderedDict()
    dups = []  # (订单号, [被丢弃的行...], 保留的行)
    for row in rows:
        oid = (row["订单号"] or "").strip()
        if oid == "":
            raise DataError("第 %s 行 订单号 为空" % row["_line"])
        order = {
            "订单号": oid,
            "下单时间": (row["下单时间"] or "").strip(),
            "支付时间": (row["支付时间"] or "").strip(),
            "应付总额": parse_int(row["应付总额"], "应付总额", row),
            "运费": parse_int(row["运费"], "运费", row),
            "优惠": parse_int(row["优惠"], "优惠", row),
            "_line": row["_line"],
        }
        if oid in kept:
            dups.append(kept[oid])
        kept[oid] = order
    return kept, dups


def load_items(path):
    """读取子表，按订单号分组，组内保持文件行序（用于分摊余数的稳定取舍）。"""
    rows = read_rows(path, ITEM_COLS)
    grouped = OrderedDict()
    for seq, row in enumerate(rows):
        oid = (row["订单号"] or "").strip()
        if oid == "":
            raise DataError("第 %s 行 订单号 为空" % row["_line"])
        qty = parse_int(row["数量"], "数量", row)
        price = parse_int(row["行单价"], "行单价", row)
        if qty <= 0:
            raise DataError("第 %s 行 数量 非正: %s" % (row["_line"], qty))
        if price < 0:
            raise DataError("第 %s 行 行单价 为负: %s" % (row["_line"], price))
        grouped.setdefault(oid, []).append({
            "订单号": oid,
            "商品号": (row["商品号"] or "").strip(),
            "数量": qty,
            "行单价": price,
            "行原额": qty * price,
            "_seq": seq,
            "_line": row["_line"],
        })
    return grouped


def allocate(total, weights):
    """最大余额法：把 total 按 weights 比例拆成整数，和恰为 total。

    余数按「小数部分从大到小」分配；小数部分相同按行在子表中的先后顺序，
    因此结果只取决于输入，不依赖字典/集合的遍历顺序。
    """
    if total < 0:
        raise DataError("待分摊金额为负: %s" % total)
    base = sum(weights)
    if base <= 0:
        raise DataError("分摊基数合计为 0")
    raw = [total * w for w in weights]
    parts = [r // base for r in raw]
    left = total - sum(parts)
    if left < 0 or left >= len(weights):
        raise DataError("分摊余数越界: %s" % left)
    order = sorted(range(len(weights)), key=lambda i: (-(raw[i] % base), i))
    for i in order[:left]:
        parts[i] += 1
    return parts


def build(orders, items, require_paid=True):
    """生成事实表行与核对信息。"""
    fact_rows = []
    recon_rows = []
    excluded_rows = []
    problems = []

    for oid, order in orders.items():
        lines = items.get(oid, [])
        if not lines:
            raise DataError("订单 %s 在子表里没有商品行" % oid)

        goods = sum(line["行原额"] for line in lines)
        target = goods - order["优惠"] + order["运费"]
        if target < 0:
            problems.append((oid, "应摊合计为负: %s" % target))

        if require_paid and order["支付时间"] == "":
            excluded_rows.append({
                "订单号": oid,
                "下单时间": order["下单时间"],
                "商品合计": goods,
                "优惠": order["优惠"],
                "运费": order["运费"],
                "应付总额": order["应付总额"],
                "原因": "无支付时间（未成交）",
            })
            continue

        weights = [line["行原额"] for line in lines]
        disc_parts = allocate(order["优惠"], weights)
        ship_parts = allocate(order["运费"], weights)

        net_sum = 0
        for idx, line in enumerate(lines):
            net = line["行原额"] - disc_parts[idx] + ship_parts[idx]
            net_sum += net
            fact_rows.append({
                "订单号": oid,
                "商品号": line["商品号"],
                "数量": line["数量"],
                "行原额": line["行原额"],
                "行净额": net,
                "行优惠分摊": disc_parts[idx],
                "行运费分摊": ship_parts[idx],
                "_seq": line["_seq"],
            })

        diff = net_sum - target
        if diff != 0:
            problems.append((oid, "行净额合计与应摊合计差 %s 分" % diff))

        recon_rows.append({
            "订单号": oid,
            "商品行数": len(lines),
            "商品合计": goods,
            "优惠": order["优惠"],
            "运费": order["运费"],
            "应付总额_源": order["应付总额"],
            "应摊合计": target,
            "行净额合计": net_sum,
            "差额": diff,
            "应付总额差": target - order["应付总额"],
        })

    fact_rows.sort(key=lambda r: (r["订单号"], r["_seq"]))
    recon_rows.sort(key=lambda r: r["订单号"])
    excluded_rows.sort(key=lambda r: r["订单号"])
    return fact_rows, recon_rows, excluded_rows, problems


def write_db(path, fact_rows):
    tmp = path + ".tmp"
    for candidate in (tmp, path):
        if os.path.exists(candidate):
            os.remove(candidate)
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript(
            """
            CREATE TABLE order_line_fact (
                订单号      TEXT    NOT NULL,
                商品号      TEXT    NOT NULL,
                数量        INTEGER NOT NULL,
                行原额      INTEGER NOT NULL,
                行净额      INTEGER NOT NULL,
                行优惠分摊  INTEGER NOT NULL,
                行运费分摊  INTEGER NOT NULL,
                CHECK (行净额 = 行原额 - 行优惠分摊 + 行运费分摊)
            );
            CREATE INDEX idx_order_line_fact_order ON order_line_fact (订单号);
            """
        )
        conn.executemany(
            "INSERT INTO order_line_fact "
            "(订单号, 商品号, 数量, 行原额, 行净额, 行优惠分摊, 行运费分摊) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (r["订单号"], r["商品号"], r["数量"], r["行原额"], r["行净额"],
                 r["行优惠分摊"], r["行运费分摊"])
                for r in fact_rows
            ],
        )
        conn.commit()
    finally:
        conn.close()
    os.replace(tmp, path)


def write_csv(path, rows, columns):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row[c] for c in columns})


def main(argv=None):
    parser = argparse.ArgumentParser(description="订单级优惠/运费分摊到行，产出 SQLite 行级事实表")
    parser.add_argument("--orders", default="orders.csv")
    parser.add_argument("--items", default="order_items.csv")
    parser.add_argument("--db", default="order_fact.db")
    parser.add_argument("--recon", default="reconciliation.csv")
    parser.add_argument("--dups", default="duplicate_orders.csv")
    parser.add_argument("--excluded", default="excluded_orders.csv")
    parser.add_argument("--keep-unpaid", action="store_true",
                        help="未付款单也进事实表（默认不进）")
    args = parser.parse_args(argv)

    try:
        orders, dup_rows = load_orders(args.orders)
        items = load_items(args.items)
    except DataError as exc:
        print("数据错误: %s" % exc, file=sys.stderr)
        return 2

    extra = [oid for oid in items if oid not in orders]
    if extra:
        print("数据错误: 子表有订单号不在主表: %s" % ",".join(sorted(extra)[:10]), file=sys.stderr)
        return 2

    fact_rows, recon_rows, excluded_rows, problems = build(
        orders, items, require_paid=not args.keep_unpaid
    )

    write_db(args.db, fact_rows)
    write_csv(args.recon, recon_rows, [
        "订单号", "商品行数", "商品合计", "优惠", "运费", "应付总额_源",
        "应摊合计", "行净额合计", "差额", "应付总额差",
    ])
    dup_detail = []
    for dropped in dup_rows:
        kept = orders[dropped["订单号"]]
        dup_detail.append({
            "订单号": dropped["订单号"],
            "保留行号": kept["_line"], "保留优惠": kept["优惠"], "保留应付总额": kept["应付总额"],
            "丢弃行号": dropped["_line"], "丢弃优惠": dropped["优惠"],
            "丢弃应付总额": dropped["应付总额"],
        })
    dup_detail.sort(key=lambda r: r["订单号"])
    write_csv(args.dups, dup_detail, [
        "订单号", "保留行号", "保留优惠", "保留应付总额",
        "丢弃行号", "丢弃优惠", "丢弃应付总额",
    ])
    write_csv(args.excluded, excluded_rows, [
        "订单号", "下单时间", "商品合计", "优惠", "运费", "应付总额", "原因",
    ])

    bad = [r for r in recon_rows if r["差额"] != 0]
    src_diff = [r for r in recon_rows if r["应付总额差"] != 0]
    print("主表行数 %d，去重后订单 %d（重复 %d 单）" % (
        len(orders) + len(dup_rows), len(orders), len(dup_rows)))
    print("进事实表订单 %d，商品行 %d；未进表 %d 单（未付款）" % (
        len(recon_rows), len(fact_rows), len(excluded_rows)))
    print("差一分的单: %d；应付总额与应摊合计不符的单: %d" % (len(bad), len(src_diff)))
    print("产出: %s, %s, %s, %s" % (args.db, args.recon, args.dups, args.excluded))

    if problems:
        for oid, msg in problems:
            print("  异常 %s: %s" % (oid, msg), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
