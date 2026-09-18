import importlib.util
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = ROOT / "src" / "agentos" / "skills" / "bundled" / "video-merger"
VM_PATH = SRC_DIR / "src" / "video_merger.py"

spec = importlib.util.spec_from_file_location("video_merger", VM_PATH)
vm_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vm_module)
VideoMerger = vm_module.VideoMerger


def test_merge_creates_parent_output_directory(tmp_path: Path):
    with patch.object(VideoMerger, "_check_dependencies"):
        merger = VideoMerger(ffmpeg_path="ffmpeg", ffprobe_path="ffprobe")
    input_dir = str(tmp_path / "inputs")
    os.makedirs(input_dir, exist_ok=True)
    out_file = tmp_path / "nested" / "subfolder" / "final.mp4"

    assert not out_file.parent.exists()

    with (
        patch.object(merger, "get_sorted_videos", return_value=["dummy1.mp4"]),
        patch.object(merger, "get_video_info", return_value=(1920, 1080, 10.0)),
        patch("subprocess.run") as mock_run,
    ):
        def fake_run(cmd, **kwargs):
            if str(out_file) in cmd:
                # Simulate ffmpeg writing output
                out_file.write_bytes(b"dummy video data")
            return MagicMock(returncode=0)

        mock_run.side_effect = fake_run

        success = merger.merge(input_dir=input_dir, output_path=str(out_file))

        assert success is True
        assert out_file.parent.exists()
        assert out_file.is_file()
