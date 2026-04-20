
# DAC: Digital Asset Controller (v1.0)

> **Clean your media library without breaking a sweat.**
> A dual-purpose powerhouse designed to automate the most tedious parts of managing your MKV collection.

-----

## 🚀 What is DAC?

DAC (Digital Asset Controller) combines two essential media tools into one easy-to-use interface. Whether you want to fix wonky audio tracks or turn messy file names into a beautiful, organized library, DAC handles the heavy lifting.

### 1\. MKV Track Forge

Stop opening every file in a player just to change the language.

  * **Instant Defaults:** Set your preferred audio and subtitle tracks as the "Default."
  * **No Re-encoding:** Uses `mkvpropedit` to modify headers. Your video quality stays 100% identical, and the process takes seconds, not hours.
  * **Batch Power:** Fix an entire season or movie collection in one click.

### 2\. TV Show Renamer

Turn `Show.S01E01.720p.x264-RELEASEx.mkv` into `Show Name - S01E01 - Pilot.mkv` automatically.

  * **Smart Detection:** Uses the TVmaze API to find real episode titles.
  * **Anime Support:** Handles absolute episode numbering common in anime.
  * **Preview Mode:** See exactly what the files will look like before you commit to the change.

-----

## 🛠️ Getting Started

### Prerequisites

1.  **Python 3.9+** (Download from [python.org](https://www.python.org/))
2.  **MKVToolNix** (Required for MKV Forge)
      * [Download here](https://mkvtoolnix.download)
      * *Tip: Ensure it's installed so DAC can find its engine.*

### Installation

Clone the repo and install the web dashboard requirements:

```bash
git clone https://github.com/Compromisee/DAC.git
cd DAC
pip install flask flask-cors Pillow
```

### Running the App

```bash
python main.py
```

This will launch a small control window. Click **"Open Dashboard"** to manage your files in a sleek, modern web interface.

-----

## 🖥️ How to Use

### Using MKV Track Forge

1.  **Add Files:** Drag and drop or select a folder of MKV files.
2.  **Scan:** Let DAC analyze the internal tracks (languages, formats, etc.).
3.  **Choose Mode:**
      * **Auto English:** Automatically finds and sets English audio/subs as default.
      * **Manual:** You pick exactly which track index should be the primary.
4.  **Process:** Hit start and watch your library get organized.

### Using TV Show Renamer

1.  **Select Folder:** Point DAC to your messy TV show folder.
2.  **Search:** Confirm the show name via the TVmaze search bar.
3.  **Match:** DAC will align your local files with the official episode list.
4.  **Rename:** Click to apply the new, clean naming convention.

-----

## 📊 The Dashboard

Control everything from your favorite browser. The web UI includes:

  * **The Forge:** Deep control over track metadata.
  * **The Renamer:** Searchable database for episode metadata.
  * **Analytics:** Real-time stats and charts showing your library processing progress.

-----

## ⌨️ For Power Users (CLI)

Prefer the terminal? DAC includes a robust Command Line Interface.

```bash
# Example: Process a folder in "Auto English" mode and move them to a new location
python main.py --mode auto_english --output /media/sorted --move /media/downloads/*.mkv
```

**Common Commands:**

  * `--headless`: Run the web server without the GUI window.
  * `--cli`: Start an interactive command-line session.
  * `--dry`: Run a "test" to see what would happen without changing any files.

-----

## 📁 Output Examples

### MKV Forge (Auto-Organize)

If you use the `Auto English` mode with an output folder, DAC sorts your files based on what it found:

  * `NoEngAudio/` — Files missing an English audio track.
  * `NoEngSub/` — Files missing English subtitles.
  * `NoEnglish/` — Files with neither.

### TV Renamer (Naming Convention)

Your files will follow this clean, industry-standard format:

> `Show Name - S01E01 - [2024-01-01] - Episode Title.mkv`

-----

# Caught errors
  🔴 - Not fixed
  🟡 - Working on fix
  🟢 - Resolved  

- 🟡 Drag and drop or upload doesnt work
        - Use PATH

## 🤝 Contributing & Support

Found a bug or have a feature request?

  * **GitHub:** [Compromisee/DAC](https://github.com/Compromisee/DAC.git)
  * **Issues:** Please report any track detection issues on the GitHub Issues page.

*Built for collectors, by collectors.*
