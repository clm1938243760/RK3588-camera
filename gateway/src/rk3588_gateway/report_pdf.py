from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional, Union

from PIL import Image, ImageDraw, ImageFont

from .config import ReportPdfConfig

LOGGER = logging.getLogger(__name__)


class ReportPdfConverter:
    def __init__(self, config: ReportPdfConfig) -> None:
        self.config = config
        self.output_dir = Path(config.output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def convert(self, source: Union[str, Path], source_type: str) -> Optional[Path]:
        if not self.config.enabled:
            return None
        path = Path(source)
        if not path.exists() or not path.is_file():
            LOGGER.warning("report source missing: %s", path)
            return None

        target = self._target_path(path, source_type)
        try:
            if self._is_pdf(path):
                shutil.copy2(path, target)
            elif self._is_postscript(path) and self._ps_to_pdf(path, target):
                pass
            elif self._image_to_pdf(path, target):
                pass
            elif self._office_to_pdf(path, target):
                pass
            elif self._text_to_pdf(path, target):
                pass
            else:
                self._placeholder_pdf(path, target, source_type)
            LOGGER.info("report pdf ready: %s -> %s", path, target)
            return target
        except Exception:
            LOGGER.exception("report pdf conversion failed: %s", path)
            return None

    def _target_path(self, source: Path, source_type: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in source.stem)[:60]
        target = self.output_dir / f"{stamp}_{source_type}_{safe_name}.pdf"
        index = 1
        while target.exists():
            target = self.output_dir / f"{stamp}_{source_type}_{safe_name}_{index}.pdf"
            index += 1
        return target

    def _is_pdf(self, path: Path) -> bool:
        with path.open("rb") as handle:
            return handle.read(5) == b"%PDF-"

    def _is_postscript(self, path: Path) -> bool:
        with path.open("rb") as handle:
            head = handle.read(8192)
        return head.startswith(b"%!") or b"%!PS" in head

    def _ps_to_pdf(self, source: Path, target: Path) -> bool:
        ps2pdf = shutil.which("ps2pdf")
        if not ps2pdf:
            return False
        with source.open("rb") as handle:
            head = handle.read(8192)
        marker = head.find(b"%!PS")
        convert_source = source
        temp_name = None
        try:
            if marker > 0:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".ps") as temp:
                    temp_name = Path(temp.name)
                    temp.write(head[marker:])
                    with source.open("rb") as handle:
                        handle.seek(len(head))
                        shutil.copyfileobj(handle, temp)
                convert_source = temp_name
            result = subprocess.run(
                [ps2pdf, str(convert_source), str(target)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
            )
            if result.returncode != 0:
                LOGGER.warning("ps2pdf failed: %s", result.stderr.strip())
                return False
            return target.exists()
        finally:
            if temp_name:
                try:
                    temp_name.unlink()
                except FileNotFoundError:
                    pass

    def _image_to_pdf(self, source: Path, target: Path) -> bool:
        try:
            with Image.open(source) as image:
                if image.mode in ("RGBA", "LA"):
                    background = Image.new("RGB", image.size, "white")
                    background.paste(image, mask=image.getchannel("A"))
                    image = background
                else:
                    image = image.convert("RGB")
                image.save(target, "PDF", resolution=100.0)
            return True
        except Exception:
            return False

    def _office_to_pdf(self, source: Path, target: Path) -> bool:
        if source.suffix.lower() not in {
            ".doc",
            ".docx",
            ".xls",
            ".xlsx",
            ".ppt",
            ".pptx",
            ".odt",
            ".ods",
            ".odp",
            ".rtf",
        }:
            return False
        soffice = shutil.which("libreoffice") or shutil.which("soffice")
        if not soffice:
            LOGGER.warning("libreoffice not found, cannot convert office file: %s", source)
            return False
        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run(
                [
                    soffice,
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    temp_dir,
                    str(source),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
            )
            if result.returncode != 0:
                LOGGER.warning("libreoffice conversion failed: %s", result.stderr.decode(errors="replace").strip())
                return False
            produced = Path(temp_dir) / (source.stem + ".pdf")
            if not produced.exists():
                matches = list(Path(temp_dir).glob("*.pdf"))
                produced = matches[0] if matches else produced
            if not produced.exists():
                return False
            shutil.copy2(produced, target)
            return True

    def _text_to_pdf(self, source: Path, target: Path) -> bool:
        try:
            text = source.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                text = source.read_text(encoding="gb18030")
            except UnicodeDecodeError:
                return False

        pages = self._text_pages(text)
        pages[0].save(target, "PDF", save_all=True, append_images=pages[1:])
        return True

    def _placeholder_pdf(self, source: Path, target: Path, source_type: str) -> None:
        stat = source.stat()
        lines = [
            "Unable to convert this source into a visual PDF.",
            "",
            f"Source: {source_type}",
            f"File: {source.name}",
            f"Size: {stat.st_size} bytes",
            "",
            "The original file was kept for later processing with a dedicated parser or driver.",
        ]
        pages = self._text_pages("\n".join(lines))
        pages[0].save(target, "PDF", save_all=True, append_images=pages[1:])

    def _text_pages(self, text: str) -> list[Image.Image]:
        font = self._font(24)
        small = self._font(18)
        lines = []
        for raw in text.splitlines() or [""]:
            while len(raw) > 32:
                lines.append(raw[:32])
                raw = raw[32:]
            lines.append(raw)

        pages = []
        for offset in range(0, len(lines), 26):
            image = Image.new("RGB", (1240, 1754), "white")
            draw = ImageDraw.Draw(image)
            y = 70
            for line in lines[offset : offset + 26]:
                draw.text((70, y), line, font=font, fill="black")
                y += 58
            draw.text((70, 1680), "RK3588 Gateway", font=small, fill="#666666")
            pages.append(image)
        return pages or [Image.new("RGB", (1240, 1754), "white")]

    def _font(self, size: int):
        for path in (
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ):
            if Path(path).exists():
                return ImageFont.truetype(path, size)
        return ImageFont.load_default()
