from lineage.ingest.parse import parse, read_indexable

COBOL = b"""       IDENTIFICATION DIVISION.
       PROGRAM-ID. BILLRUN.
       PROCEDURE DIVISION.
       MAIN-PARA.
           PERFORM CALC-TAX.
           STOP RUN.
       CALC-TAX.
           COMPUTE WS-TAX = WS-AMOUNT * 0.18.
"""


def test_legacy_encodings(tmp_path):
    cp1252 = "' Rechnungsbetrag für Kunden – netto\nSub Total()\nEnd Sub\n".encode("cp1252")
    assert "für" in parse("Billing.bas", cp1252).summary + parse("Billing.bas", cp1252).chunks[0].text

    utf16 = tmp_path / "Form1.frm"
    utf16.write_bytes("' Order entry form\r\nSub Save()\r\nEnd Sub\r\n".encode("utf-16"))
    data = read_indexable(utf16, 1_000_000)  # has NUL bytes, but a BOM: text, not binary
    assert data is not None and "Order entry form" in parse("Form1.frm", data).chunks[0].text


def test_cobol_is_parsed():
    parsed = parse("src/BILLRUN.cbl", COBOL)
    assert parsed.language == "cobol"
    assert any("CALC-TAX" in c.text for c in parsed.chunks)


def test_long_routine_is_fully_searchable():
    body = "\n".join(f"    total = total + item_{i}" for i in range(1500))
    src = f"def giant(items):\n    total = 0\n{body}\n    marker_at_the_very_end = 1\n    return total\n".encode()
    parsed = parse("legacy/giant.py", src)
    parts = [c for c in parsed.chunks if c.symbol is not None]
    assert len(parts) > 20 and all(len(c.text) <= 3000 for c in parts)
    assert any("marker_at_the_very_end" in c.text for c in parts)
    assert parts[-1].end_line == 1504


def test_code_between_symbols_is_indexed():
    script = "\n".join(["def a():", "    pass"] + [f"print('step {i}')" for i in range(60)] + ["def b():", "    pass"])
    parsed = parse("deploy/run.py", script.encode())
    assert any("step 59" in c.text and c.symbol is None for c in parsed.chunks)
