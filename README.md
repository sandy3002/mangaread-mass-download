# 📚 Manga Downloader

Download entire manga series from **mangaread.org** and compile them into a single PDF.

## Features

- 🔍 Automatically detects all chapters from a manga series URL
- ⬇️ Downloads all chapter images with concurrent workers
- 📕 Compiles everything into a single PDF
- 📄 Optional per-chapter PDFs
- 🔄 Retry logic for failed downloads
- 🎨 Beautiful terminal UI with progress bars
- ⚡ Configurable chapter range (download only what you need)

## Installation

```bash
pip install -r requirements.txt
```

## Usage

### Download entire series:
```bash
python manga_download.py https://www.mangaread.org/manga/omniscient-readers-viewpoint/
```

### Download specific chapter range:
```bash
python manga_download.py https://www.mangaread.org/manga/omniscient-readers-viewpoint/ --start 1 --end 10
```

### Also create per-chapter PDFs:
```bash
python manga_download.py https://www.mangaread.org/manga/omniscient-readers-viewpoint/ --chapter-pdf
```

### Custom output directory:
```bash
python manga_download.py https://www.mangaread.org/manga/omniscient-readers-viewpoint/ -o ~/Desktop/manga
```

### Keep images after PDF creation:
```bash
python manga_download.py https://www.mangaread.org/manga/omniscient-readers-viewpoint/ --no-cleanup
```

## Options

| Flag | Short | Description |
|------|-------|-------------|
| `--start` | `-s` | Start chapter number (1-indexed) |
| `--end` | `-e` | End chapter number (1-indexed) |
| `--output` | `-o` | Output directory path |
| `--chapter-pdf` | | Create individual PDFs per chapter |
| `--no-cleanup` | | Keep downloaded images after PDF creation |
| `--workers` | `-w` | Concurrent image downloads (default: 4) |


**Just a fun project I made with antigravity**
