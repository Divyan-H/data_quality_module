"""Upload and identifier validation: everything a browser sends is untrusted (src/security/uploads.py)."""
import pytest

from src.security.uploads import (DATA_EXTENSIONS, WORKBOOK_EXTENSIONS, UploadRejected, safe_filename,
                                  safe_relative_path, unique_name, valid_artifact_id, valid_run_id)


@pytest.mark.parametrize("name", ["spend.xlsx", "Client A 2026.XLSX", "data.v2.csv", "a.parquet", "x.tsv"])
def test_accepts_supported_names(name):
    assert safe_filename(name, DATA_EXTENSIONS) == name


@pytest.mark.parametrize("name", ["", None, "   ", ".xlsx", "a.exe", "a.zip", "noext", "a.xlsx.exe", "con.xlsx", "NUL.csv",
                                  "a" * 200 + ".xlsx", "bad<name>.xlsx", 'q"uote.csv', "tab\tname.csv"])
def test_rejects_unsafe_or_unsupported_names(name):
    with pytest.raises(UploadRejected):
        safe_filename(name, DATA_EXTENSIONS)


def test_a_path_is_reduced_to_its_file_name():
    assert safe_filename("../../etc/passwd.csv", DATA_EXTENSIONS) == "passwd.csv"
    assert safe_filename("C:\\Users\\x\\spend.xlsx", DATA_EXTENSIONS) == "spend.xlsx"


def test_review_workbooks_must_be_xlsx():
    assert safe_filename("Hygiene_Review.xlsx", WORKBOOK_EXTENSIONS)
    for bad in ("review.csv", "review.xls", "review.xlsm"):
        with pytest.raises(UploadRejected):
            safe_filename(bad, WORKBOOK_EXTENSIONS)


def test_relative_paths_keep_folders_but_never_escape():
    assert safe_relative_path("Client A/2026/spend.xlsx", "spend.xlsx") == "Client A/2026/spend.xlsx"
    assert safe_relative_path("Client A\\2026\\spend.xlsx", "spend.xlsx") == "Client A/2026/spend.xlsx"
    assert safe_relative_path(None, "spend.xlsx") == "spend.xlsx"
    assert safe_relative_path("", "spend.xlsx") == "spend.xlsx"
    for bad in ("../x/spend.xlsx", "a/../../spend.xlsx", "a/" * 12 + "b.xlsx", "a/b:c/d.xlsx"):
        with pytest.raises(UploadRejected):
            safe_relative_path(bad, "spend.xlsx")


def test_a_dot_component_is_harmless_and_dropped():
    assert safe_relative_path("a/./b.xlsx", "b.xlsx") == "a/b.xlsx"


def test_a_leading_slash_is_dropped_not_followed():
    assert safe_relative_path("/etc/spend.xlsx", "spend.xlsx") == "etc/spend.xlsx"


def test_duplicate_names_are_numbered():
    taken = {"a.xlsx", "a (2).xlsx"}
    assert unique_name("a.xlsx", taken) == "a (3).xlsx"
    assert unique_name("b.xlsx", taken) == "b.xlsx"


def test_run_ids_are_hex_only():
    assert valid_run_id("0123456789abcdef")
    for bad in ("../x", "run", "0123456789ABCDEF", "", "a" * 40, "0123456789abcde/", None, 5):
        with pytest.raises(UploadRejected):
            valid_run_id(bad)


def test_artifact_ids_are_slugs():
    assert valid_artifact_id("data-b-ab12_0_t1")
    for bad in ("../x", "A", "x y", "", "-start", "a/b", "x" * 100):
        with pytest.raises(UploadRejected):
            valid_artifact_id(bad)
