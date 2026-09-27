import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import xlsxwriter

from app.clean_io import (
    EXCEL_MAX_CHARS,
    CleanIoError,
    CsvStream,
    cell_text,
    detect_encoding,
    estimate_rows_from_size,
    file_kind,
    inspect_file,
    list_input_files,
    looks_like_formula,
    normalize_headers,
    open_reader,
    open_writer,
    precount,
    sniff_delimiter,
)
from app.clean_models import InputOptions, OutputOptions


class TempCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, text, encoding="utf-8"):
        path = self.tmp / name
        path.write_bytes(text.encode(encoding) if isinstance(text, str) else text)
        return path

    def write_xlsx(self, name, rows, sheet="Sheet1"):
        path = self.tmp / name
        book = xlsxwriter.Workbook(str(path), {"constant_memory": True})
        try:
            worksheet = book.add_worksheet(sheet)
            for r, row in enumerate(rows):
                for c, value in enumerate(row):
                    if value is None:
                        continue
                    worksheet.write_string(r, c, str(value))
        finally:
            book.close()
        return path


class DetectionTests(TempCase):
    def test_bom_wins_over_statistics(self):
        # UTF-16 无 BOM 时会被统计法猜成 cp1252 并解出「看起来合理」的乱码，所以
        # BOM 必须是第一判据。这条守住那个顺序。
        head = "a,b\n1,2\n".encode("utf-16")  # encode('utf-16') 自带 BOM
        encoding, confidence, _ = detect_encoding(head)
        self.assertEqual(encoding, "utf-16")
        self.assertEqual(confidence, 1.0)

    def test_utf16_without_bom_uses_nul_pattern(self):
        raw = "a,b\n1,2\n".encode("utf-16-le")  # 不含 BOM
        encoding, confidence, warnings = detect_encoding(raw)
        self.assertEqual(encoding, "utf-16-le")
        self.assertTrue(warnings)

    def test_utf8_sig(self):
        self.assertEqual(detect_encoding(b"\xef\xbb\xbfa,b\n")[0], "utf-8-sig")

    def test_plain_utf8(self):
        self.assertEqual(detect_encoding("名字,城市\n".encode("utf-8"))[0], "utf-8")

    def test_gbk_detected(self):
        encoding, confidence, _ = detect_encoding("名字,城市\n张三,北京\n".encode("gbk"))
        self.assertIn(encoding.lower(), {"gbk", "gb18030", "gb2312"})

    def test_utf8_survives_a_sample_boundary_inside_a_character(self):
        """头部样本固定 256KB，这个边界与字符边界无关。

        只要它落在一个三字节字的中间，严格解码就失败，探测会掉进统计推断并把纯 UTF-8 的
        中文文件判成 gb18030 —— 列名与全部数据变乱码且不报错。这是规模测试在 1.25GB 文件
        上抓到的真 bug，所以要有这条回归。

        构造：表头在最前面（否则表头自己就落在样本之外了），第二行是一长串 'a' 加一个
        「中」，「中」的三个字节正好跨过 262144 这个边界，第三行是正常数据。
        """
        from app.clean_io import read_head

        limit = 262_144
        header = "名字,城市\n"
        filler = "a" * (limit - len(header.encode("utf-8")) - 1) + "中"
        path = self.write("b.csv", f"{header}{filler}\n张三,北京\n".encode("utf-8"))

        head = read_head(path, limit)
        self.assertEqual(len(head), limit)
        # 边界确实切断了那个字符：样本最后一个字节是 UTF-8 前导字节，单独解不出来
        self.assertGreaterEqual(head[-1], 0xE0)
        self.assertIn("�", head.decode("utf-8", errors="replace"))

        self.assertEqual(detect_encoding(head)[0], "utf-8")
        detection = inspect_file(path)
        self.assertEqual(detection.encoding, "utf-8")
        # 这就是那个 bug 的表现：旧代码下这里是 '鍚嶅瓧'/'鍩庡競'（UTF-8 字节按 GBK 解）。
        self.assertEqual(detection.headers, ["名字", "城市"])

        # 真正读一遍：读侧不受 256KB 样本限制，能看到整行数据。
        reader = open_reader(path, InputOptions(), batch_rows=10)
        try:
            self.assertEqual(reader.columns, ["名字", "城市"])
            rows = []
            while True:
                batch = reader.read_batch()
                if batch is None:
                    break
                rows.extend(batch)
        finally:
            reader.close()
        self.assertEqual(rows[-1], ["张三", "北京"])

    def test_invalid_utf8_in_the_middle_is_not_excused(self):
        # 容忍只针对末尾被切断的那一个字符，中间的坏字节必须如实判成「不是 UTF-8」。
        from app.clean_io import _tail_tolerant_decode

        raw = "名字,城市\n".encode("utf-8") + b"\xff\xfe" + b"ok\n"
        self.assertIsNone(_tail_tolerant_decode(raw, "utf-8"))


class DelimiterTests(unittest.TestCase):
    def test_comma(self):
        delimiter, confidence = sniff_delimiter("a,b,c\n1,2,3\n4,5,6\n")
        self.assertEqual(delimiter, ",")
        self.assertEqual(confidence, 1.0)

    def test_semicolon(self):
        self.assertEqual(sniff_delimiter("a;b;c\n1;2;3\n4;5;6\n")[0], ";")

    def test_tab(self):
        self.assertEqual(sniff_delimiter("a\tb\tc\n1\t2\t3\n")[0], "\t")

    def test_commas_inside_tab_separated_fields(self):
        # 制表符文件里字段自己含逗号：逗号切出的列数会抖动，稳定性判据要选对制表符。
        text = "name\tnote\nA\tx,y,z\nB\tp,q\nC\ta,b,c,d,e\n"
        self.assertEqual(sniff_delimiter(text)[0], "\t")

    def test_quoted_newlines_do_not_break_scoring(self):
        text = 'a,b\n1,"第一行\n第二行"\n2,"x,y"\n3,plain\n'
        self.assertEqual(sniff_delimiter(text)[0], ",")

    def test_single_column_returns_none(self):
        delimiter, confidence = sniff_delimiter("just\nlines\nhere\n")
        self.assertIsNone(delimiter)
        self.assertEqual(confidence, 0.0)

    def test_tie_prefers_comma(self):
        # 逗号和分号都能切出稳定的 3 列时，选更常见的逗号。
        self.assertEqual(sniff_delimiter("a,b;c\n1,2;3\n4,5;6\n")[0], ",")


class InspectTests(TempCase):
    def test_csv_sample_and_header(self):
        path = self.write("a.csv", "id,name\n1,张三\n2,李四\n3,王五\n")
        detection = inspect_file(path, sample_rows=2)
        self.assertEqual(detection.kind, "csv")
        self.assertEqual(detection.headers, ["id", "name"])
        self.assertEqual(detection.sample_rows, [["1", "张三"], ["2", "李四"]])
        self.assertEqual(detection.delimiter, ",")
        self.assertGreater(detection.row_estimate, 0)

    def test_header_row_offset(self):
        path = self.write("a.csv", "垃圾行\nid,name\n1,张三\n")
        detection = inspect_file(path, InputOptions(header_row=1))
        self.assertEqual(detection.headers, ["id", "name"])
        self.assertEqual(detection.sample_rows, [["1", "张三"]])

    def test_ragged_rows_are_reported_at_config_time(self):
        # 参差行只有样本扫描能提前告诉用户 —— 全量读是静默补齐的，没有条数。
        path = self.write("a.csv", "a,b,c\n1,2,3\n4,5\n6,7,8,9\n")
        detection = inspect_file(path)
        self.assertEqual(detection.ragged_rows, 2)
        self.assertTrue(any("字段数" in w for w in detection.warnings))

    def test_empty_file_is_a_clear_error(self):
        path = self.write("empty.csv", "")
        with self.assertRaises(CleanIoError):
            inspect_file(path)

    def test_header_only_file(self):
        path = self.write("h.csv", "a,b\n")
        detection = inspect_file(path)
        self.assertEqual(detection.headers, ["a", "b"])
        self.assertEqual(detection.sample_rows, [])

    def test_duplicate_headers_survive(self):
        path = self.write("d.csv", "id,id,name\n1,2,x\n")
        detection = inspect_file(path)
        self.assertEqual(len(detection.headers), 3)

    def test_formula_looking_cells_warn(self):
        path = self.write("f.csv", "a,b\n=SUM(A1),ok\n+1,x\n")
        detection = inspect_file(path)
        self.assertTrue(any("公式" in w for w in detection.warnings))

    def test_gbk_headers_and_sample(self):
        # 非 UTF-8 就地解码，不再有「要不要转存」这个状态（见模块 docstring 第 1 条）。
        # 只断言落在 GB 家族内：gb18030 是 gbk 的超集，探测出前者同样正确。
        path = self.write("g.csv", "名字,城市\n张三,北京\n", encoding="gbk")
        detection = inspect_file(path)
        self.assertIn(detection.encoding.lower(), {"gbk", "gb18030", "gb2312"})
        self.assertEqual(detection.headers, ["名字", "城市"])
        self.assertEqual(detection.sample_rows, [["张三", "北京"]])

    def test_xlsx_sample_and_height(self):
        path = self.write_xlsx("a.xlsx", [["id", "name"], ["1", "张三"], ["2", "李四"], ["3", "王五"]])
        detection = inspect_file(path, sample_rows=2)
        self.assertEqual(detection.kind, "xlsx")
        self.assertEqual(detection.headers, ["id", "name"])
        self.assertEqual(detection.sample_rows[0], ["1", "张三"])
        # total_height 才是整表行数：height 只会是本次 load 的行数。
        self.assertEqual(detection.row_estimate, 3)

    def test_xlsx_leading_zeros_preserved(self):
        path = self.write_xlsx("z.xlsx", [["id"], ["007"], ["008"]])
        detection = inspect_file(path)
        self.assertEqual(detection.sample_rows, [["007"], ["008"]])

    def test_jsonl(self):
        path = self.write("a.jsonl", '{"a":1,"b":"x"}\n{"a":2}\n')
        detection = inspect_file(path)
        self.assertEqual(detection.headers, ["a", "b"])
        self.assertEqual(detection.sample_rows[0], [1, "x"])
        self.assertEqual(detection.sample_rows[1], [2, None])

    def test_unknown_suffix_is_rejected(self):
        path = self.write("a.xls", "x")
        with self.assertRaises(CleanIoError):
            inspect_file(path)
        self.assertEqual(file_kind(path, allow_unknown=True), "unknown")


class ReaderTests(TempCase):
    def read_all(self, path, **kwargs):
        options = kwargs.pop("options", InputOptions())
        reader = open_reader(path, options, batch_rows=kwargs.pop("batch_rows", 2))
        rows = []
        try:
            while True:
                batch = reader.read_batch()
                if batch is None:
                    break
                rows.extend(batch)
        finally:
            reader.close()
        return reader, rows

    def test_csv_batches_and_values_stay_strings(self):
        path = self.write("a.csv", "id,score\n007,1e3\n008,2\n")
        reader, rows = self.read_all(path)
        self.assertEqual(reader.columns, ["id", "score"])
        # 这两行如果被推断成类型就会变成 7 和 1000.0 —— 静默改数据。
        self.assertEqual(rows, [["007", "1e3"], ["008", "2"]])

    def test_csv_skip_is_exact(self):
        path = self.write("a.csv", "n\n" + "".join(f"{i}\n" for i in range(10)))
        reader = open_reader(path, InputOptions(), batch_rows=3)
        reader.skip(7)
        rows = []
        while True:
            batch = reader.read_batch()
            if batch is None:
                break
            rows.extend(batch)
        reader.close()
        self.assertEqual([row[0] for row in rows], ["7", "8", "9"])

    def test_csv_quoted_newlines_and_crlf(self):
        path = self.write("a.csv", 'a,b\r\n1,"第一行\n第二行"\r\n2,x\r\n')
        _, rows = self.read_all(path)
        self.assertEqual(rows[0], ["1", "第一行\n第二行"])

    def test_csv_ragged_lines_are_padded_and_truncated(self):
        path = self.write("a.csv", "a,b,c\n1,2,3\n4,5\n6,7,8,9\n")
        reader, rows = self.read_all(path)
        self.assertEqual(reader.columns, ["a", "b", "c"])
        self.assertEqual(rows[1], ["4", "5", ""])
        self.assertEqual(rows[2], ["6", "7", "8"])
        # 精确计数：polars 的 truncate_ragged_lines 只能静默补齐，报不出这个数。
        self.assertEqual(reader.ragged_rows, 2)
        self.assertTrue(any("表头宽度" in note for note in reader.notes))

    def test_empty_strings_stay_empty_not_null(self):
        path = self.write("a.csv", "a,b\n1,\n,2\n")
        _, rows = self.read_all(path)
        self.assertEqual(rows, [["1", ""], ["", "2"]])

    def test_gbk_csv_reads_correctly_without_transcode(self):
        path = self.write("g.csv", "名字,城市\n张三,北京\n李四,上海\n", encoding="gbk")
        reader, rows = self.read_all(path)
        self.assertEqual(reader.columns, ["名字", "城市"])
        self.assertEqual(rows, [["张三", "北京"], ["李四", "上海"]])
        # 就地解码：没有转存产物，也没有替换字符。
        self.assertEqual(reader.replacement_chars, 0)
        self.assertEqual(list(self.tmp.glob("*.utf8")), [])

    def test_gbk_skip_is_exact(self):
        # 续跑要能在 GBK 上按记录跳行（旧实现依赖转存后的副本才能做到）。
        path = self.write("g.csv", "名字\n" + "".join(f"名字{i}\n" for i in range(6)), encoding="gbk")
        reader = open_reader(path, InputOptions(), batch_rows=2)
        reader.skip(4)
        rows = []
        while True:
            batch = reader.read_batch()
            if batch is None:
                break
            rows.extend(batch)
        reader.close()
        self.assertEqual([row[0] for row in rows], ["名字4", "名字5"])

    def test_undecodable_bytes_are_counted_not_silent(self):
        """errors="replace" 不抛异常，唯一能暴露「编码猜错了」的就是这个计数。

        必须**显式指定 utf-8** 才制造得出替换：不指定时探测会落到单字节编码（latin-1 之类），
        而单字节编码能把任何字节都解成「合法」字符、一个替换字符都不产生 —— 那正是编码
        错配最隐蔽的形态。用户手动指定编码是最常见的触发场景。
        """
        path = self.write("bad.csv", b"a,b\n\xff\xfe\xff,ok\n")
        reader, rows = self.read_all(
            path, batch_rows=10, options=InputOptions(encoding="utf-8")
        )
        self.assertEqual(reader.replacement_chars, 3)
        self.assertTrue(any("U+FFFD" in note for note in reader.notes))
        self.assertEqual(rows[0][1], "ok")

    def test_utf8_with_bom(self):
        path = self.write("b.csv", b"\xef\xbb\xbfid,name\n1,x\n")
        reader, rows = self.read_all(path)
        self.assertEqual(reader.columns, ["id", "name"])
        self.assertEqual(rows, [["1", "x"]])

    def test_bom_survives_forced_utf8_encoding(self):
        # 用户显式指定 utf-8 去读带 BOM 的文件时，open 不替我们剥 BOM。
        # 列名里留下 U+FEFF 会让字段映射静默全部失配，所以必须由 normalize_headers 剥掉。
        path = self.write("b.csv", b"\xef\xbb\xbfid,name\n1,x\n")
        reader, rows = self.read_all(path, options=InputOptions(encoding="utf-8"))
        self.assertEqual(reader.columns, ["id", "name"])
        self.assertEqual(rows, [["1", "x"]])

    def test_long_field_exceeds_default_csv_limit(self):
        # csv 默认字段上限 128KB。超长字段必须能读，不能抛 field larger than field limit。
        path = self.write("long.csv", "a,b\nx," + "y" * 200_000 + "\n")
        reader, rows = self.read_all(path, batch_rows=10)
        self.assertEqual(len(rows[0][1]), 200_000)

    def test_nul_bytes_pass_through(self):
        # 实测 csv 模块不因 NUL 报错（见模块 docstring），所以这是行为契约而非缺陷。
        path = self.write("nul.csv", b"a,b\nx\x00y,1\nok,2\n")
        _, rows = self.read_all(path, batch_rows=10)
        self.assertEqual(rows[0][0], "x\x00y")
        self.assertEqual(rows[1], ["ok", "2"])

    def test_parse_error_is_not_fatal_and_is_counted(self):
        """坏行必须只丢自己那一条，不能毁掉整个文件。

        靠临时压回 `field_size_limit` 来制造 `csv.Error`：实测 csv 在抛出超长字段错误后
        **会跳到下一条记录**（不会卡在原地），所以「计数 + 继续」是正确行为，而
        `MAX_CONSECUTIVE_CSV_ERRORS` 那道闸门也不会被正常数据误触发。
        """
        import csv

        path = self.write("broken.csv", "a,b\nxx,yy\n" + "z" * 200 + ",1\nok,2\n")
        original = csv.field_size_limit()
        csv.field_size_limit(50)
        self.addCleanup(csv.field_size_limit, original)
        reader, rows = self.read_all(path, batch_rows=10)
        self.assertEqual(reader.parse_errors, 1)
        self.assertTrue(any("解析失败" in note for note in reader.notes))
        # 坏行前后的好行必须都在。
        self.assertEqual(rows, [["xx", "yy"], ["ok", "2"]])

    def test_xlsx_batches(self):
        path = self.write_xlsx("a.xlsx", [["id"], ["1"], ["2"], ["3"]])
        reader, rows = self.read_all(path)
        self.assertEqual(reader.columns, ["id"])
        self.assertEqual(rows, [["1"], ["2"], ["3"]])
        self.assertEqual(reader.row_estimate, 3)

    def test_xlsx_skip(self):
        rows = [["n"]] + [[str(i)] for i in range(10)]
        path = self.write_xlsx("a.xlsx", rows)
        reader = open_reader(path, InputOptions(), batch_rows=3)
        reader.skip(7)
        got = []
        while True:
            batch = reader.read_batch()
            if batch is None:
                break
            got.extend(batch)
        reader.close()
        self.assertEqual([row[0] for row in got], ["7", "8", "9"])

    def test_xlsx_sheet_selection(self):
        path = self.tmp / "two.xlsx"
        book = xlsxwriter.Workbook(str(path))
        try:
            first = book.add_worksheet("第一")
            first.write_string(0, 0, "x")
            first.write_string(1, 0, "1")
            second = book.add_worksheet("第二")
            second.write_string(0, 0, "y")
            second.write_string(1, 0, "2")
        finally:
            book.close()
        detection = inspect_file(path)
        self.assertEqual(detection.sheet_names, ["第一", "第二"])
        reader, rows = self.read_all(path, options=InputOptions(sheet="第二"))
        self.assertEqual(reader.columns, ["y"])
        self.assertEqual(rows, [["2"]])

    def test_jsonl_batches_and_bad_lines(self):
        path = self.write("a.jsonl", '{"a":1}\nnot json\n{"a":2}\n{"a":3,"b":"n"}\n')
        reader, rows = self.read_all(path)
        self.assertEqual(reader.columns, ["a", "b"])
        self.assertEqual(rows, [[1, None], [2, None], [3, "n"]])
        self.assertEqual(reader.bad_lines, 1)
        # 后面才出现的键不该撑大表头，但要记数。
        self.assertEqual(reader.unknown_keys, 0)

    def test_jsonl_skip_counts_records_not_lines(self):
        path = self.write("a.jsonl", '{"a":1}\nbroken\n{"a":2}\n{"a":3}\n')
        reader = open_reader(path, InputOptions(), batch_rows=5)
        reader.skip(2)
        got = []
        while True:
            batch = reader.read_batch()
            if batch is None:
                break
            got.extend(batch)
        reader.close()
        self.assertEqual(got, [[3]])

    def test_parquet_roundtrip(self):
        import polars as pl

        path = self.tmp / "a.parquet"
        pl.DataFrame({"a": ["007", "x"], "b": ["1", None]}).write_parquet(path)
        reader, rows = self.read_all(path)
        self.assertEqual(reader.columns, ["a", "b"])
        self.assertEqual(rows, [["007", "1"], ["x", None]])

    def test_precount_matches_actual(self):
        path = self.write("a.csv", "a\n" + "".join(f"{i}\n" for i in range(37)))
        self.assertEqual(precount(path, InputOptions()), 37)
        # 引号里含换行：按记录数而不是按换行数 —— 数错会让进度条永远走不到 100%
        quoted = self.write("q.csv", 'a,b\n1,"x\ny"\n2,z\n')
        self.assertEqual(precount(quoted, InputOptions()), 2)

    def test_precount_gbk(self):
        path = self.write("g.csv", "名字\n张三\n李四\n", encoding="gbk")
        self.assertEqual(precount(path, InputOptions()), 2)

    def test_estimate_is_roughly_right(self):
        path = self.write("a.csv", "a,b\n" + "".join(f"{i},x{i}\n" for i in range(500)))
        estimate = estimate_rows_from_size(path, path.read_bytes(), ",")
        self.assertGreater(estimate, 300)
        self.assertLess(estimate, 800)

    def test_estimate_excludes_the_header(self):
        """表头是**已知偏移**，不是估算的一部分。

        这条口径有三处消费者：`/estimate`（用户在它上面确认才提交）、进度条分母、以及
        精确计数 `precount`。三处不一致时的表现是「预估 301 行、跑完 300 行」和进度条在
        最后一刻跳一下 —— 差 1 也伤可信度，而用户正是在预估上做决定。
        """
        path = self.write("h.csv", "名字,城市\n" + "".join(f"u{i},北京\n" for i in range(100)))
        self.assertEqual(precount(path, InputOptions()), 100)
        self.assertEqual(estimate_rows_from_size(path, path.read_bytes(), ",", skip_rows=1), 100)
        self.assertEqual(inspect_file(path).row_estimate, 100)
        # header_row=1（第一行是垃圾、第二行是表头）时偏移是 2
        offset = self.write("o.csv", "垃圾\n名字,城市\nu1,北京\nu2,上海\n")
        self.assertEqual(precount(offset, InputOptions(header_row=1)), 2)
        self.assertEqual(inspect_file(offset, InputOptions(header_row=1)).row_estimate, 2)

    def test_jsonl_skip_and_precount_agree(self):
        """JSONL 的 header_row 是「跳过开头 N 条记录」，精确计数与预估必须同一口径。"""
        path = self.write("a.jsonl", '{"meta":1}\n{"a":1}\n{"a":2}\n{"a":3}\n')
        self.assertEqual(precount(path, InputOptions(header_row=1)), 3)
        self.assertEqual(inspect_file(path, InputOptions(header_row=1)).row_estimate, 3)


class CsvStreamTests(TempCase):
    """`CsvStream` 是「探测与读取一致」这条契约的唯一实现处，单独测它。"""

    def stream(self, path, **kwargs):
        # 测试里也要关文件：否则 ResourceWarning 会淹没真正有价值的警告。
        stream = CsvStream(path, **kwargs)
        self.addCleanup(stream.close)
        return stream

    def test_header_row_offset(self):
        path = self.write("a.csv", "垃圾行\n名字,城市\n张三,北京\n")
        stream = self.stream(path, header_row=1)
        self.assertEqual(stream.columns, ["名字", "城市"])
        self.assertEqual(list(stream), [["张三", "北京"]])

    def test_skip_counts_records_not_lines(self):
        # 引号里含换行时「第 2 行」不等于「第 2 个换行」—— 只有解析才数得准。
        path = self.write("a.csv", 'n,v\n0,"含\n换行"\n1,b\n2,c\n')
        stream = self.stream(path, skip=1)
        self.assertEqual(list(stream), [["1", "b"], ["2", "c"]])

    def test_set_skip_after_iteration_is_rejected(self):
        path = self.write("a.csv", "n\n1\n2\n")
        stream = self.stream(path)
        list(stream)
        with self.assertRaises(CleanIoError):
            stream.set_skip(1)

    def test_empty_file_raises_with_clear_message(self):
        path = self.write("a.csv", "")
        with self.assertRaises(CleanIoError):
            self.stream(path).open()

    def test_header_only_file_yields_nothing(self):
        path = self.write("a.csv", "a,b\n")
        self.assertEqual(list(self.stream(path)), [])

    def test_columns_available_without_explicit_open(self):
        # 属性访问不该有「先调 open() 否则拿到空列表」这个前置条件。
        path = self.write("a.csv", "a,b\n1,2\n")
        self.assertEqual(self.stream(path).columns, ["a", "b"])

    def test_unnamed_and_duplicate_headers_survive(self):
        path = self.write("a.csv", "a,,a\n1,2,3\n")
        stream = self.stream(path)
        # 空列名补占位名，重名保持原样（下游靠位置区分，不靠名字去重）
        self.assertEqual(stream.columns, ["a", "未命名列2", "a"])


class SpoolTests(TempCase):
    """spool 是 xlsx/parquet 输出的中间层，续跑全靠它可追加。写读必须严格对称。"""

    def crash(self, writer):
        """模拟进程崩溃：放掉底层句柄，**不调用 `close()`**。

        这是续跑测试的关键动作 —— 正常 `close()` 会转格式并删掉 spool，而崩溃时它根本没
        跑过，spool 与 `.part` 原样留在盘上。这两种状态必须分清楚，否则测的是不存在的流程。
        """
        writer._sink.close()

    def test_spool_roundtrip_is_byte_exact(self):
        # 写用 csv.writer、读用 csv.reader，往返必须逐字节等价 —— 中间夹一层别的解析器
        # 就会多一层语义差异（这也是不用 polars 读 spool 的原因）。
        tricky = [
            ["含,逗号", '含"引号', "含\n换行"],
            [" 前后空格 ", "=SUM(A1)", ""],
            ["中文", "emoji🙂", "tab\there"],
        ]
        path = self.tmp / "o.xlsx"
        writer = open_writer(path, ["a", "b", "c"], OutputOptions(format="xlsx"))
        writer.write(tricky)
        writer.flush()
        # 不 close（close 会转成 xlsx 并删掉 spool），直接读 spool 验证对称性。
        records = list(writer._spool())
        writer.close()
        self.assertEqual(records, tricky)

    def test_xlsx_and_parquet_share_the_spool_path(self):
        for fmt in ("xlsx", "parquet"):
            with self.subTest(fmt=fmt):
                path = self.tmp / f"o.{fmt}"
                writer = open_writer(path, ["a", "b"], OutputOptions(format=fmt))
                writer.write([["1", "x"], ["2", "y"]])
                writer.close()
                self.assertTrue(path.is_file())
                # spool 与中间产物都不能留下
                self.assertEqual(list(self.tmp.glob(f"o.{fmt}.part*")), [])
                self.assertEqual(list(self.tmp.glob("*.part.csv")), [])

    def test_spool_truncate_and_append_matches_uninterrupted(self):
        """续跑写侧契约：截到提交点再追加，spool 内容必须与一次写完逐字节相同。

        直接比对 spool（而不是最终的 xlsx 字节）是因为 xlsx 里含生成时间戳之类的元数据，
        字节不同不代表数据不同。
        """
        path = self.tmp / "o.xlsx"

        clean = open_writer(path, ["a"], OutputOptions(format="xlsx"))
        clean.write([["1"], ["2"]])
        clean.flush()
        clean.write([["3"]])
        expected = list(clean._spool())
        clean.close()

        interrupted = open_writer(path, ["a"], OutputOptions(format="xlsx"))
        interrupted.write([["1"], ["2"]])
        committed = interrupted.flush()
        interrupted.write([["3"]])   # 已写出但未提交
        self.crash(interrupted)

        resumed = open_writer(
            path, ["a"], OutputOptions(format="xlsx"), append_bytes=committed
        )
        # 未提交的 "3" 必须被截掉，否则会写出 1,2,3,3
        self.assertEqual(list(resumed._spool()), [["1"], ["2"]])
        resumed.write([["3"]])
        self.assertEqual(list(resumed._spool()), expected)
        resumed.close()

    def test_xlsx_row_count_after_crash_resume(self):
        import fastexcel

        path = self.tmp / "o.xlsx"
        writer = open_writer(path, ["a"], OutputOptions(format="xlsx"))
        writer.write([["1"], ["2"]])
        committed = writer.flush()
        writer.write([["3"]])
        self.crash(writer)

        resumed = open_writer(
            path, ["a"], OutputOptions(format="xlsx"), append_bytes=committed
        )
        resumed.write([["3"]])
        resumed.close()

        frame = fastexcel.read_excel(str(path)).load_sheet(0).to_polars()
        self.assertEqual(frame.height, 3)
        self.assertEqual(frame["a"].to_list(), ["1", "2", "3"])

    def test_csv_writer_truncates_uncommitted_tail(self):
        # 同样的契约在直写格式上：截断不能推给调用方，忘了就是静默重复几行。
        path = self.tmp / "o.csv"
        writer = open_writer(path, ["a"], OutputOptions(format="csv"))
        writer.write([["1"], ["2"]])
        committed = writer.flush()
        writer.write([["3"]])
        self.crash(writer)

        resumed = open_writer(path, ["a"], OutputOptions(format="csv"), append_bytes=committed)
        resumed.write([["3"]])
        resumed.close()
        self.assertEqual(path.read_text("utf-8"), "a\n1\n2\n3\n")

    def test_append_bytes_beyond_file_size_is_rejected(self):
        # 状态说写了 100 字节、文件只有 4 字节 —— 悄悄从头写会让用户以为产物是续着的。
        path = self.tmp / "o.csv"
        path.write_text("a\n1\n", encoding="utf-8")
        with self.assertRaises(CleanIoError):
            open_writer(path, ["a"], OutputOptions(format="csv"), append_bytes=100)

    def test_append_bytes_with_missing_product_is_rejected(self):
        with self.assertRaises(CleanIoError):
            open_writer(
                self.tmp / "gone.csv", ["a"], OutputOptions(format="csv"), append_bytes=100
            )


class WriterTests(TempCase):
    def test_csv_roundtrip_and_header(self):
        path = self.tmp / "out.csv"
        writer = open_writer(path, ["a", "b"], OutputOptions(format="csv"))
        writer.write([["1", "x"], ["2", "y"]])
        size = writer.flush()
        self.assertGreater(size, 0)
        writer.close()
        self.assertEqual(path.read_text("utf-8"), "a,b\n1,x\n2,y\n")

    def test_csv_append_after_truncate(self):
        # 断点续跑的写侧契约：把文件截到提交点再追加，结果必须与不中断一致。
        path = self.tmp / "out.csv"
        writer = open_writer(path, ["a"], OutputOptions(format="csv"))
        writer.write([["1"], ["2"]])
        committed = writer.flush()
        writer.close()

        with open(path, "r+b") as handle:
            handle.truncate(committed)
        writer = open_writer(path, ["a"], OutputOptions(format="csv"), append_bytes=committed)
        writer.write([["3"]])
        writer.close()
        self.assertEqual(path.read_text("utf-8"), "a\n1\n2\n3\n")

    def test_suspend_keeps_the_commit_point_and_can_continue(self):
        """取消时的手势：`suspend()` 把缓冲刷出去，但**不动提交坐标**。

        续跑的坐标是提交点（`flush()` 的返回值），不是「磁盘上现在有多少字节」。挂起之后
        磁盘上通常比提交点长一段未提交的行（关句柄必须刷缓冲），续跑时 `append_bytes` 会
        先把它截掉。这条测试把「写一半 → 挂起 → 续写」与「一口气写完」对齐到同一份字节。
        """
        straight = self.tmp / "straight.csv"
        writer = open_writer(straight, ["a"], OutputOptions(format="csv"))
        writer.write([["1"], ["2"], ["3"]])
        writer.close()

        path = self.tmp / "out.csv"
        writer = open_writer(path, ["a"], OutputOptions(format="csv"))
        writer.write([["1"], ["2"]])
        committed = writer.flush()
        writer.write([["3"]])  # 提交点之后那一批：写进缓冲，尚未提交
        writer.suspend()
        self.assertGreater(path.stat().st_size, committed)

        writer = open_writer(path, ["a"], OutputOptions(format="csv"), append_bytes=committed)
        writer.write([["3"]])
        writer.close()
        self.assertEqual(path.read_bytes(), straight.read_bytes())

    def test_suspend_keeps_the_spool_and_never_produces_the_final_file(self):
        """xlsx/parquet 的最终产物只在 `close()` 时生成，挂起必须介于 `close` 与 `abort` 之间。

        `close()` 会把只写了一半的 spool 转成一份「看起来完整」的残缺表（用户会下载它，
        然后以为任务跑完了）；`abort()` 把 spool 删掉，续跑就没有可接的东西。所以
        `suspend()` 只关句柄：目标格式**不存在**、spool **还在**、且 spool 里比提交点多
        一段未提交的行。
        """
        for fmt in ("xlsx", "parquet"):
            with self.subTest(fmt=fmt):
                path = self.tmp / f"out.{fmt}"
                spool = self.tmp / f"out.{fmt}.part.csv"
                writer = open_writer(path, ["a"], OutputOptions(format=fmt))
                writer.write([["1"]])
                committed = writer.flush()
                writer.write([["2"]])
                writer.suspend()
                self.assertFalse(path.exists())
                self.assertFalse((self.tmp / f"out.{fmt}.part").exists())
                self.assertTrue(spool.exists())
                self.assertGreater(spool.stat().st_size, committed)

                writer = open_writer(
                    path, ["a"], OutputOptions(format=fmt), append_bytes=committed
                )
                writer.write([["3"]])
                writer.close()
                self.assertFalse(spool.exists())
                if fmt == "xlsx":
                    self.assertEqual(inspect_file(path).sample_rows, [["1"], ["3"]])
                else:
                    import polars as pl

                    self.assertEqual(pl.read_parquet(path).rows(), [("1",), ("3",)])

    def test_suspend_is_idempotent(self):
        """引擎在取消路径上会挂起两次：取消处理里一次，`finally` 里又一次。

        第二次必须是空操作 —— 句柄已经关了，再关一次不能抛异常。这里只要求「不抛」，
        内容由上面两条测试守着。
        """
        for fmt in ("csv", "jsonl", "xlsx", "parquet"):
            with self.subTest(fmt=fmt):
                writer = open_writer(self.tmp / f"out.{fmt}", ["a"], OutputOptions(format=fmt))
                writer.write([["1"]])
                writer.flush()
                writer.suspend()
                writer.suspend()

    def test_formula_sanitized_and_counted_in_csv(self):
        path = self.tmp / "out.csv"
        counters = {}
        writer = open_writer(path, ["a"], OutputOptions(format="csv"), counters=counters)
        writer.write([["=SUM(A1)"], ["normal"], ["+1"], ["-2"], ["@x"]])
        writer.close()
        self.assertEqual(counters["formula_sanitized"], 4)
        self.assertIn("'=SUM(A1)", path.read_text("utf-8"))
        self.assertIn("normal", path.read_text("utf-8"))

    def test_formula_sanitize_can_be_disabled(self):
        path = self.tmp / "out.csv"
        writer = open_writer(
            path, ["a"], OutputOptions(format="csv"), sanitize_formula=False
        )
        writer.write([["=SUM(A1)"]])
        writer.close()
        self.assertEqual(path.read_text("utf-8"), "a\n=SUM(A1)\n")

    def test_xlsx_writes_formula_text_not_formulas(self):
        # write() 会把 =SUM(...) 当公式写进去、回读变 '0'。必须原样保留文本。
        path = self.tmp / "out.xlsx"
        counters = {}
        writer = open_writer(path, ["a", "b"], OutputOptions(format="xlsx"), counters=counters)
        writer.write([["=SUM(A1:A9)", "正常"], ["+1", "x"]])
        writer.close()
        detection = inspect_file(path)
        self.assertEqual(detection.sample_rows, [["=SUM(A1:A9)", "正常"], ["+1", "x"]])
        # xlsx 侧用 write_string，值不需要被改，所以不该计入 formula_sanitized。
        self.assertNotIn("formula_sanitized", counters)

    def test_xlsx_long_cell_truncation_is_counted(self):
        path = self.tmp / "out.xlsx"
        counters = {}
        writer = open_writer(path, ["a"], OutputOptions(format="xlsx"), counters=counters)
        writer.write([["x" * (EXCEL_MAX_CHARS + 100)]])
        writer.close()
        self.assertEqual(counters["cell_truncated"], 1)
        rows = inspect_file(path, sample_rows=1).sample_rows
        self.assertEqual(len(rows[0][0]), EXCEL_MAX_CHARS)

    def test_jsonl_roundtrip(self):
        path = self.tmp / "out.jsonl"
        writer = open_writer(path, ["a", "b"], OutputOptions(format="jsonl"))
        writer.write([["1", "中文"], ["", None]])
        writer.close()
        import orjson

        lines = [orjson.loads(line) for line in path.read_text("utf-8").splitlines()]
        # JSONL 是有类型的格式，None 就该写成 null —— 强行写 "" 反而丢掉了「缺失」这个
        # 信息。（CSV 没有 null，那边 None → 空串。）空串仍原样保留为空串。
        self.assertEqual(lines, [{"a": "1", "b": "中文"}, {"a": "", "b": None}])

    def test_parquet_roundtrip(self):
        path = self.tmp / "out.parquet"
        writer = open_writer(path, ["a", "b"], OutputOptions(format="parquet"))
        writer.write([["1", "x"], ["2", "y"]])
        writer.close()
        import polars as pl

        frame = pl.read_parquet(path)
        self.assertEqual(frame.columns, ["a", "b"])
        self.assertEqual(frame.rows(), [("1", "x"), ("2", "y")])

    def test_abort_leaves_nothing_behind(self):
        path = self.tmp / "out.xlsx"
        writer = open_writer(path, ["a"], OutputOptions(format="xlsx"))
        writer.write([["1"]])
        writer.flush()
        writer.abort()
        self.assertFalse(path.exists())
        self.assertFalse((self.tmp / "out.xlsx.part.csv").exists())

    def test_empty_values_round_trip_as_empty(self):
        path = self.tmp / "out.csv"
        writer = open_writer(path, ["a", "b"], OutputOptions(format="csv"))
        writer.write([["", ""]])
        writer.close()
        _, rows = ReaderTests.read_all(self, path)
        self.assertEqual(rows, [["", ""]])


class CellTextTests(unittest.TestCase):
    def test_none_is_empty(self):
        self.assertEqual(cell_text(None), "")

    def test_bool_is_lowercase(self):
        self.assertEqual(cell_text(True), "true")
        self.assertEqual(cell_text(False), "false")

    def test_integral_float_loses_point_zero(self):
        self.assertEqual(cell_text(1.0), "1")
        self.assertEqual(cell_text(-2.0), "-2")

    def test_fractional_float_keeps_precision(self):
        self.assertEqual(cell_text(1.5), "1.5")

    def test_strings_pass_through(self):
        self.assertEqual(cell_text("007"), "007")

    def test_formula_detection(self):
        # 任何以 = + - @ 开头的非空文本都算疑似公式。宁可多前缀一个引号，也不要漏掉
        # 一个能被 Excel 当公式执行的单元格。
        for text in ("=1+1", "+x", "-x", "@x", "=", "= "):
            self.assertTrue(looks_like_formula(text), text)
        for text in ("", "x", "1", " x", "1+1", "a=b"):
            self.assertFalse(looks_like_formula(text), text)


class DirectoryTests(TempCase):
    def test_recursive_listing_is_sorted_and_filtered(self):
        (self.tmp / "sub").mkdir()
        self.write("b.csv", "a\n1\n")
        self.write("a.csv", "a\n1\n")
        self.write("sub/c.csv", "a\n1\n")
        self.write("skip.txt", "x")
        self.write("note.txt", "x")
        files = list_input_files([self.tmp], InputOptions(recursive=True))
        self.assertEqual([path.name for path in files], ["a.csv", "b.csv", "c.csv"])

    def test_non_recursive(self):
        (self.tmp / "sub").mkdir()
        self.write("a.csv", "a\n1\n")
        self.write("sub/c.csv", "a\n1\n")
        files = list_input_files([self.tmp], InputOptions(recursive=False))
        self.assertEqual([path.name for path in files], ["a.csv"])

    def test_custom_include(self):
        self.write("a.csv", "a\n1\n")
        self.write("b.xlsx", "x")
        files = list_input_files([self.tmp], InputOptions(include=["*.csv"]))
        self.assertEqual([path.name for path in files], ["a.csv"])

    def test_missing_path_errors(self):
        with self.assertRaises(CleanIoError):
            list_input_files([self.tmp / "nope"], InputOptions())

    def test_single_file_input(self):
        path = self.write("a.csv", "a\n1\n")
        self.assertEqual(list_input_files([path], InputOptions()), [path.resolve()])


if __name__ == "__main__":
    unittest.main()
