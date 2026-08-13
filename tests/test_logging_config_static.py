import re
from pathlib import Path


def test_main_reduces_noisy_third_party_loggers():
    source = Path("main.py").read_text()
    assert 'logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)' in source
    assert 'logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)' in source
    assert "ApschedulerOverlapFilter" in source
    assert 'logging.getLogger("apscheduler.scheduler").addFilter(ApschedulerOverlapFilter())' in source
    assert 'logging.getLogger("httpx").setLevel(logging.WARNING)' in source
    assert "app.add_error_handler(_error_handler)" in source


def test_pinecone_dependency_uses_current_package():
    # The package must be the modern ``pinecone`` (the deprecated
    # ``pinecone-client`` is a different distribution). A version pin on the
    # correct package is fine — and wanted, for reproducible builds.
    requirements = Path("requirements.txt").read_text()
    pyproject = Path("pyproject.toml").read_text()
    assert "pinecone-client" not in requirements
    assert "pinecone-client" not in pyproject
    assert re.search(r"^pinecone(\s*$|[=<>!\[])", requirements, re.MULTILINE)
    assert re.search(r'"pinecone([=<>!\["])', pyproject)
