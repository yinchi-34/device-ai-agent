import json
import subprocess
import sys
from pathlib import Path

EXPECTED_TABLES = {
    "basic_capability",
    "capability_command_template",
    "capability_point_binding",
    "device_category",
    "device_category_capability",
    "device_command",
    "device_connection",
    "device_instance",
    "device_model",
    "device_point",
    "device_profile_version",
    "document_chunk",
    "document_chunk_embedding",
    "profile_extraction_candidate",
    "profile_extraction_run",
    "device_protocol_spec",
    "profile_capability",
    "source_document",
}


def test_importing_domain_packages_registers_alembic_metadata():
    project_dir = Path(__file__).parents[1]
    script = (
        "import json; "
        "from app.db.base import Base; "
        "from app.modules import device, document, testing; "
        "print(json.dumps(sorted(Base.metadata.tables)))"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project_dir,
        capture_output=True,
        text=True,
        check=True,
    )

    assert set(json.loads(result.stdout)) == EXPECTED_TABLES
