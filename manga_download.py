#!/usr/bin/env python3
"""
Manga Downloader - Downloads manga chapters from mangaread.org and compiles into PDF.

Usage:
    python manga_download.py <manga_url> [options]

Example:
    python manga_download.py https://www.mangaread.org/manga/omniscient-readers-viewpoint/
    python manga_download.py https://www.mangaread.org/manga/omniscient-readers-viewpoint/ --start 1 --end 10
    python manga_download.py https://www.mangaread.org/manga/omniscient-readers-viewpoint/ --chapter-pdf
"""

import argparse
import os
import re
import sys
import time
import shutil
import concurrent.futures
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from PIL import Image
import img2pdf
from rich.console import Console
from rich.progress import (
    Progress,
    SpinnerColumn,
    BarColumn,
    TextColumn,
    TimeRemainingColumn,
    MofNCompleteColumn,
    TaskProgressColumn,
)
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

console = Console()

# ─── Constants ────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.mangaread.org/",
}

MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds
DEFAULT_DOWNLOAD_WORKERS = 4  # concurrent image downloads per chapter
REQUEST_TIMEOUT = 30


# ─── Helper Functions ─────────────────────────────────────────────────────────


def create_session() -> requests.Session:
    """Create a requests session with default headers and retry logic."""
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def safe_filename(name: str) -> str:
    """Sanitize a string to be safe for use as a filename."""
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name


def fetch_page(session: requests.Session, url: str) -> BeautifulSoup:
    """Fetch a page and return parsed BeautifulSoup object with retries."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            return BeautifulSoup(resp.text, "lxml")
        except requests.RequestException as e:
            if attempt < MAX_RETRIES - 1:
                console.print(
                    f"  [yellow]⚠ Retry {attempt + 1}/{MAX_RETRIES} for {url}: {e}[/yellow]"
                )
                time.sleep(RETRY_DELAY * (attempt + 1))
            else:
                console.print(f"  [red]✗ Failed to fetch {url}: {e}[/red]")
                raise


def download_image(
    session: requests.Session, url: str, save_path: Path
) -> bool:
    """Download a single image with retries. Returns True on success."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = session.get(url.strip(), timeout=REQUEST_TIMEOUT, stream=True)
            resp.raise_for_status()

            with open(save_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            # Validate the image can be opened
            try:
                with Image.open(save_path) as img:
                    img.verify()
            except Exception:
                save_path.unlink(missing_ok=True)
                raise ValueError(f"Invalid image data from {url}")

            return True

        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))
            else:
                console.print(f"    [red]✗ Failed: {url} — {e}[/red]")
                return False

    return False


# ─── Core Logic ───────────────────────────────────────────────────────────────


def get_manga_info(session: requests.Session, manga_url: str) -> dict:
    """Extract manga title and chapter list from the main manga page."""
    console.print(f"\n[cyan]🔍 Fetching manga info from:[/cyan] {manga_url}\n")

    soup = fetch_page(session, manga_url)

    # Extract title
    title_el = soup.select_one(".post-title h1")
    if not title_el:
        title_el = soup.select_one("h1")
    title = title_el.get_text(strip=True) if title_el else "Unknown Manga"

    # Extract chapter links — Madara theme stores chapters in list items
    chapter_links = []
    chapter_list = soup.select(".wp-manga-chapter a")

    for a_tag in chapter_list:
        href = a_tag.get("href", "").strip()
        text = a_tag.get_text(strip=True)
        if href and "/chapter" in href.lower():
            chapter_links.append({"url": href, "title": text})

    # Chapters are listed newest-first on the page; reverse for chronological order
    chapter_links.reverse()

    # Remove duplicates while preserving order
    seen = set()
    unique_chapters = []
    for ch in chapter_links:
        if ch["url"] not in seen:
            seen.add(ch["url"])
            unique_chapters.append(ch)
    chapter_links = unique_chapters

    return {
        "title": title,
        "url": manga_url,
        "chapters": chapter_links,
    }


def get_chapter_images(session: requests.Session, chapter_url: str) -> list[str]:
    """Extract all image URLs from a single chapter page."""
    soup = fetch_page(session, chapter_url)

    image_urls = []

    # Primary selector: Madara theme manga images
    img_tags = soup.select(".reading-content img.wp-manga-chapter-img")

    if not img_tags:
        # Fallback: try broader selectors
        img_tags = soup.select(".reading-content img")

    if not img_tags:
        # Last resort: look for any image in page-break divs
        img_tags = soup.select(".page-break img")

    for img in img_tags:
        # Prefer data-src (lazy-loaded) over src
        url = img.get("data-src") or img.get("data-lazy-src") or img.get("src", "")
        url = url.strip()

        if url and not url.startswith("data:"):
            # Make relative URLs absolute
            if url.startswith("//"):
                url = "https:" + url
            elif url.startswith("/"):
                url = urljoin(chapter_url, url)
            image_urls.append(url)

    return image_urls


def download_chapter(
    session: requests.Session,
    chapter: dict,
    chapter_dir: Path,
    progress: Progress,
    task_id,
) -> list[Path]:
    """Download all images for a single chapter. Returns list of image paths."""
    image_urls = get_chapter_images(session, chapter["url"])

    if not image_urls:
        console.print(
            f"    [yellow]⚠ No images found for {chapter['title']}[/yellow]"
        )
        return []

    chapter_dir.mkdir(parents=True, exist_ok=True)
    downloaded_paths = [None] * len(image_urls)

    def _download_one(idx_url):
        idx, url = idx_url
        ext = os.path.splitext(url.split("?")[0])[1] or ".jpg"
        save_path = chapter_dir / f"{idx + 1:04d}{ext}"
        if download_image(session, url, save_path):
            return idx, save_path
        return idx, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
        futures = {
            pool.submit(_download_one, (i, url)): i
            for i, url in enumerate(image_urls)
        }
        for future in concurrent.futures.as_completed(futures):
            idx, path = future.result()
            downloaded_paths[idx] = path
            progress.advance(task_id)

    # Filter out failed downloads
    return [p for p in downloaded_paths if p is not None]


def images_to_pdf(image_paths: list[Path], output_pdf: Path) -> None:
    """Convert a list of images to a single PDF file, converting non-JPEG/PNG as needed."""
    if not image_paths:
        return

    processed_paths = []

    for img_path in image_paths:
        try:
            with Image.open(img_path) as img:
                # Convert RGBA/Palette images to RGB for PDF compatibility
                if img.mode in ("RGBA", "P", "LA"):
                    rgb_img = img.convert("RGB")
                    converted_path = img_path.with_suffix(".conv.jpg")
                    rgb_img.save(converted_path, "JPEG", quality=95)
                    processed_paths.append(str(converted_path))
                elif img.mode != "RGB" and img.mode != "L":
                    rgb_img = img.convert("RGB")
                    converted_path = img_path.with_suffix(".conv.jpg")
                    rgb_img.save(converted_path, "JPEG", quality=95)
                    processed_paths.append(str(converted_path))
                else:
                    processed_paths.append(str(img_path))
        except Exception as e:
            console.print(f"    [yellow]⚠ Skipping bad image {img_path.name}: {e}[/yellow]")
            continue

    if not processed_paths:
        console.print("    [red]✗ No valid images to create PDF[/red]")
        return

    # Determine target width from the first image to make all pages equal width
    target_width = None
    try:
        with Image.open(processed_paths[0]) as first_img:
            # target_width in points (1 pt = 1 px if we assume 72 dpi)
            target_width = float(first_img.width)
    except Exception:
        pass

    # Use img2pdf for lossless PDF creation (no re-encoding of JPEG/PNG)
    try:
        if target_width:
            layout_fun = img2pdf.get_layout_fun(pagesize=(target_width, None))
            pdf_bytes = img2pdf.convert(processed_paths, layout_fun=layout_fun)
        else:
            pdf_bytes = img2pdf.convert(processed_paths)
            
        with open(output_pdf, "wb") as f:
            f.write(pdf_bytes)
    except Exception:
        # Fallback: use Pillow if img2pdf fails
        images = []
        for p in processed_paths:
            try:
                img = Image.open(p).convert("RGB")
                if target_width and img.width != int(target_width):
                    new_height = int((target_width / img.width) * img.height)
                    img = img.resize((int(target_width), new_height), Image.Resampling.LANCZOS)
                images.append(img)
            except Exception:
                continue

        if images:
            images[0].save(output_pdf, "PDF", save_all=True, append_images=images[1:])
            for img in images:
                img.close()

    # Clean up converted files
    for p in processed_paths:
        if p.endswith(".conv.jpg"):
            Path(p).unlink(missing_ok=True)


def display_manga_info(manga_info: dict, start: int, end: int) -> None:
    """Display manga information in a pretty table."""
    table = Table(
        title=f"📖  {manga_info['title']}",
        box=box.ROUNDED,
        title_style="bold magenta",
        border_style="bright_blue",
        show_lines=False,
    )
    table.add_column("Property", style="cyan", width=15)
    table.add_column("Value", style="white")

    total = len(manga_info["chapters"])
    table.add_row("Total Chapters", str(total))
    table.add_row("Downloading", f"Chapter {start} → {end}")
    table.add_row("Chapter Count", str(end - start + 1))

    console.print(table)
    console.print()


# ─── Main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="🔥 Manga Downloader — Download manga and compile to PDF",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s https://www.mangaread.org/manga/omniscient-readers-viewpoint/
  %(prog)s https://www.mangaread.org/manga/omniscient-readers-viewpoint/ --start 1 --end 10
  %(prog)s https://www.mangaread.org/manga/omniscient-readers-viewpoint/ --chapter-pdf
  %(prog)s https://www.mangaread.org/manga/omniscient-readers-viewpoint/ -o ~/Desktop/manga
        """,
    )
    parser.add_argument("url", help="Manga series URL from mangaread.org")
    parser.add_argument(
        "--start", "-s", type=int, default=None, help="Start chapter number (1-indexed)"
    )
    parser.add_argument(
        "--end", "-e", type=int, default=None, help="End chapter number (1-indexed)"
    )
    parser.add_argument(
        "--output",
        "-o",
        type=str,
        default=None,
        help="Output directory (default: ./downloads/<manga-title>)",
    )
    parser.add_argument(
        "--chapter-pdf",
        action="store_true",
        help="Also create individual PDFs per chapter",
    )
    parser.add_argument(
        "--no-cleanup",
        action="store_true",
        help="Keep downloaded images after PDF creation",
    )
    parser.add_argument(
        "--workers",
        "-w",
        type=int,
        default=DEFAULT_DOWNLOAD_WORKERS,
        help=f"Concurrent image downloads per chapter (default: {DEFAULT_DOWNLOAD_WORKERS})",
    )

    args = parser.parse_args()

    # ── Banner ──
    banner = Text()
    banner.append("  ╔══════════════════════════════════════╗\n", style="bright_blue")
    banner.append("  ║   ", style="bright_blue")
    banner.append("📚 MANGA DOWNLOADER", style="bold bright_white")
    banner.append("              ║\n", style="bright_blue")
    banner.append("  ║   ", style="bright_blue")
    banner.append("Download & Compile to PDF", style="dim white")
    banner.append("        ║\n", style="bright_blue")
    banner.append("  ╚══════════════════════════════════════╝", style="bright_blue")
    console.print(banner)

    download_workers = args.workers

    session = create_session()

    # ── Step 1: Get manga info ──
    try:
        manga_info = get_manga_info(session, args.url)
    except Exception as e:
        console.print(f"[red]✗ Failed to fetch manga info: {e}[/red]")
        sys.exit(1)

    if not manga_info["chapters"]:
        console.print("[red]✗ No chapters found. Check the URL and try again.[/red]")
        sys.exit(1)

    total_chapters = len(manga_info["chapters"])

    # ── Determine range ──
    start_idx = (args.start - 1) if args.start else 0
    end_idx = args.end if args.end else total_chapters

    start_idx = max(0, start_idx)
    end_idx = min(total_chapters, end_idx)

    if start_idx >= end_idx:
        console.print("[red]✗ Invalid chapter range.[/red]")
        sys.exit(1)

    chapters_to_download = manga_info["chapters"][start_idx:end_idx]

    display_manga_info(manga_info, start_idx + 1, end_idx)

    # ── Output directory ──
    manga_title_safe = safe_filename(manga_info["title"])
    if args.output:
        output_dir = Path(args.output)
    else:
        output_dir = Path("downloads") / manga_title_safe

    output_dir.mkdir(parents=True, exist_ok=True)
    images_dir = output_dir / "chapters"
    images_dir.mkdir(parents=True, exist_ok=True)

    # ── Step 2: Download chapters ──
    all_image_paths = []  # ordered list of (chapter_title, [image_paths])

    console.print("[bold green]⬇  Downloading chapters...[/bold green]\n")

    for ch_idx, chapter in enumerate(chapters_to_download):
        ch_num = start_idx + ch_idx + 1
        chapter_title_safe = safe_filename(chapter["title"])
        chapter_dir = images_dir / f"{ch_num:04d}_{chapter_title_safe}"

        console.print(
            f"  [cyan]({ch_idx + 1}/{len(chapters_to_download)})[/cyan] "
            f"[bold]{chapter['title']}[/bold]"
        )

        # First, get image count for progress bar
        try:
            image_urls = get_chapter_images(session, chapter["url"])
        except Exception as e:
            console.print(f"    [red]✗ Failed to load chapter: {e}[/red]")
            continue

        if not image_urls:
            console.print(f"    [yellow]⚠ No images found, skipping[/yellow]")
            continue

        chapter_dir.mkdir(parents=True, exist_ok=True)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=30),
            MofNCompleteColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=True,
        ) as progress:
            dl_task = progress.add_task(
                "    Downloading images", total=len(image_urls)
            )

            downloaded = [None] * len(image_urls)

            def _dl(idx_url):
                idx, url = idx_url
                ext = os.path.splitext(url.split("?")[0])[1] or ".jpg"
                save_path = chapter_dir / f"{idx + 1:04d}{ext}"
                if download_image(session, url, save_path):
                    return idx, save_path
                return idx, None

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=download_workers
            ) as pool:
                futures = {
                    pool.submit(_dl, (i, url)): i
                    for i, url in enumerate(image_urls)
                }
                for future in concurrent.futures.as_completed(futures):
                    idx, path = future.result()
                    downloaded[idx] = path
                    progress.advance(dl_task)

        valid_paths = [p for p in downloaded if p is not None]
        console.print(
            f"    [green]✓ {len(valid_paths)}/{len(image_urls)} images downloaded[/green]"
        )

        all_image_paths.append((chapter["title"], valid_paths))

        # ── Optional: per-chapter PDF ──
        if args.chapter_pdf and valid_paths:
            ch_pdf_path = output_dir / f"{ch_num:04d}_{chapter_title_safe}.pdf"
            images_to_pdf(valid_paths, ch_pdf_path)
            console.print(f"    [blue]📄 Chapter PDF: {ch_pdf_path.name}[/blue]")

        # Be polite to the server
        time.sleep(0.5)

    # ── Step 3: Compile combined PDF ──
    console.print(f"\n[bold green]📕 Compiling combined PDF...[/bold green]")

    combined_images = []
    for _, paths in all_image_paths:
        combined_images.extend(paths)

    if not combined_images:
        console.print("[red]✗ No images were downloaded. Cannot create PDF.[/red]")
        sys.exit(1)

    combined_pdf_name = f"{manga_title_safe}.pdf"
    combined_pdf_path = output_dir / combined_pdf_name

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=30),
        console=console,
        transient=True,
    ) as progress:
        pdf_task = progress.add_task(
            f"  Creating {combined_pdf_name}", total=None
        )
        images_to_pdf(combined_images, combined_pdf_path)
        progress.update(pdf_task, completed=True)

    # ── Cleanup images ──
    if not args.no_cleanup:
        console.print("[dim]  🧹 Cleaning up downloaded images...[/dim]")
        shutil.rmtree(images_dir, ignore_errors=True)

    # ── Summary ──
    pdf_size = combined_pdf_path.stat().st_size
    size_str = (
        f"{pdf_size / (1024 * 1024):.1f} MB"
        if pdf_size > 1024 * 1024
        else f"{pdf_size / 1024:.1f} KB"
    )

    summary = Table(
        title="✅  Download Complete",
        box=box.ROUNDED,
        title_style="bold green",
        border_style="green",
    )
    summary.add_column("", style="cyan", width=15)
    summary.add_column("", style="white")
    summary.add_row("Manga", manga_info["title"])
    summary.add_row("Chapters", f"{len(all_image_paths)} chapters")
    summary.add_row("Total Images", str(len(combined_images)))
    summary.add_row("PDF Size", size_str)
    summary.add_row("Output", str(combined_pdf_path.absolute()))

    console.print()
    console.print(summary)
    console.print()


if __name__ == "__main__":
    main()
