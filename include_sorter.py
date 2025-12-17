import argparse
import re
from pathlib import Path
import subprocess
import pickle
import sys
import os

# Define where to save the cache (same folder as the script)
CACHE_FILE = Path(__file__).parent / "header_map.pkl"

# ==========================================
# 1. CONFIGURATION (Dynamic)
# ==========================================

# Assuming the script runs from within the project or you adjust this path
# If you want this to automatically find the root from where you run the script,
# you can use Path.cwd() instead.
PROJECT_ROOT = Path(r"<ENTER PROJECT ROOT PATH HERE>")
ENGINE_ROOT = Path(r"<Enter ENGINE ROOT PATH HERE>") 

DIRS_TO_SEARCH = [
    PROJECT_ROOT / "Source",
    PROJECT_ROOT / "Plugins",
    ENGINE_ROOT / "Source",
    ENGINE_ROOT / "Plugins"
]

# Global Map: filename -> list of paths
header_map = {}

# ==========================================
# 2. CORE FUNCTIONS (Indexer & Resolver)
# ==========================================

def get_git_changed_files(mode="working", target_branch=None):
    """
    Returns a list of ABSOLUTE paths for .cpp/.h files changed in git.
    
    Modes:
      - 'staged':  git diff --cached (Files added to index)
      - 'working': git diff HEAD    (Modified files in working copy)
      - 'mr':      git diff branch...HEAD (Files changed in this branch vs remote)
    """
    try:
        # 1. Get the absolute path of the Git Root
        root_cmd = ["git", "rev-parse", "--show-toplevel"]
        git_root_str = subprocess.check_output(root_cmd, encoding="utf-8").strip()
        git_root = Path(git_root_str)

        # 2. Determine Git Command based on mode
        if mode == "staged":
            diff_cmd = ["git", "diff", "--name-only", "--cached"]
        elif mode == "mr" and target_branch:
            # Triple dot (...) finds the common ancestor, effectively showing 
            # only changes introduced in your branch relative to the target.
            diff_cmd = ["git", "diff", "--name-only", f"{target_branch}...HEAD"]
        else:
            # Default: Working directory changes vs HEAD
            diff_cmd = ["git", "diff", "--name-only", "HEAD"]

        print(f"   (Running: {' '.join(diff_cmd)})")
        result = subprocess.check_output(diff_cmd, encoding="utf-8")
        files = result.splitlines()
        
        # 3. Convert to Absolute Paths and Filter
        absolute_paths = []
        for f in files:
            # Filter for C++ source files
            if f.endswith(('.cpp', '.h', '.hpp', '.c', '.cc')):
                full_path = git_root / f
                # Ensure file actually exists (it might have been deleted)
                if full_path.exists():
                    absolute_paths.append(str(full_path))
                
        return absolute_paths
        
    except subprocess.CalledProcessError:
        print("⚠️ Error: Not a git repository, git not found, or invalid branch name.")
        return []

def index_project(force_reindex=False):
    """Scans directories OR loads from cache to build header_map."""
    global header_map
    
    # A. Try to Load Cache
    if not force_reindex and CACHE_FILE.exists():
        try:
            print("--- ⚡ Loading Index from Cache... ---")
            with open(CACHE_FILE, 'rb') as f:
                header_map.update(pickle.load(f))
            print(f"   ✅ Loaded {len(header_map)} headers instantly.")
            return
        except Exception as e:
            print(f"   ⚠️ Cache corrupted. Re-indexing... ({e})")

    # B. If no cache (or forced), Scan Disk
    print("--- 🔍 Scanning Project Files (This takes a moment)... ---")
    header_map.clear() # Reset global map
    
    for root_dir in DIRS_TO_SEARCH:
        if not root_dir.exists(): continue
            
        # Recursive scan
        for child in root_dir.rglob("*"): 
            # Skip the Intermediate and Binaries folders
            if "Intermediate" in child.parts or "Binaries" in child.parts:
                continue
            
            if child.is_file() and child.suffix in [".h", ".hpp"]:
                if child.name not in header_map:
                    header_map[child.name] = []
                header_map[child.name].append(child)
    
    # C. Save Cache for next time
    try:
        with open(CACHE_FILE, 'wb') as f:
            pickle.dump(header_map, f)
        print("   💾 Index saved to cache.")
    except Exception as e:
        print(f"   ⚠️ Could not save cache: {e}")

    print(f"--- ✅ Indexing Complete. Found {len(header_map)} unique headers. ---")
    
    
def get_include_content(line):
    match = re.search(r'#include\s+["<]([^">]+)[">]', line)
    return match.group(1) if match else None

def find_exact_match(candidates, include_string):
    clean_include = include_string.replace("\\", "/")
    for full_path in candidates:
        if full_path.as_posix().endswith(clean_include):
            return full_path
    return None

def get_nearest_neighbor(current_file, candidates):
    # Heuristic: Find candidate with longest shared path prefix
    best_candidate = None
    best_score = -1
    current_parts = current_file.parent.parts 

    for candidate in candidates:
        candidate_parts = candidate.parent.parts
        score = 0
        min_len = min(len(current_parts), len(candidate_parts))
        for i in range(min_len):
            if current_parts[i] == candidate_parts[i]:
                score += 1
            else:
                break
        if score > best_score:
            best_score = score
            best_candidate = candidate
    return best_candidate


def get_module_name(path):
    # Convert parts to lowercase to find "source" safely
    lower_parts = [p.lower() for p in path.parts]
    try:
        # Finds the FIRST "source" folder (left-to-right)
        idx = lower_parts.index("source")
        # Returns the folder immediately after it
        if idx + 1 < len(path.parts):
            return path.parts[idx + 1]
    except ValueError:
        pass
    return None


# ==========================================
# 3. FILE PROCESSOR (The Logic Logic)
# ==========================================

def sort_single_file(file_path):
    target_path = Path(file_path).resolve()
    if not target_path.exists():
        print(f"❌ Error: File not found: {file_path}")
        return

    print(f"📂 Processing: {target_path.name}")
    
    # --- 1. DETECT FORMAT (Encoding & Newline) ---
    try:
        raw_bytes = target_path.read_bytes()
        has_bom = raw_bytes.startswith(b'\xef\xbb\xbf')
        encoding = "utf-8-sig" if has_bom else "utf-8"
        original_newline = '\r\n' if b'\r\n' in raw_bytes else '\n'
    except Exception as e:
        print(f"   ⚠️ Could not read file bytes: {e}")
        return

    # --- 2. READ TEXT ---
    try:
        lines = target_path.read_text(encoding=encoding).splitlines()
    except Exception as e:
        print(f"   ⚠️ Could not decode file content: {e}")
        return
    
    # --- 3. EXTRACT INCLUDES ---
    include_lines = []
    other_lines_top = []
    other_lines_bottom = []
    
    inside_include_block = False
    finished_includes = False
    
    for line in lines:
        clean = line.strip()
        
        # Case A: Found an Include?
        if clean.startswith("#include"):
            include_lines.append(line)
            inside_include_block = True
            
        # Case B: Currently collecting includes
        elif inside_include_block:
            if not clean:
                # Ignore empty lines INSIDE the include block (we will regenerate them)
                continue
            else:
                # We hit code/comments after includes -> block is done
                finished_includes = True
                inside_include_block = False
                other_lines_bottom.append(line)
                
        # Case C: Already finished includes (Bottom of file)
        elif finished_includes:
            other_lines_bottom.append(line)
            
        # Case D: Before any includes (Copyright, Pragma, etc.)
        else:
            # FIX: Do NOT skip empty lines here. Preserve them exactly.
            other_lines_top.append(line)

    if not include_lines:
        print("   ⚠️ No includes found. Skipping.")
        return

    # --- 4. SORTING LOGIC ---
    main_header = []
    same_module = []
    other_module = {}
    plugins = {}
    engine = []
    stl = []

    current_mod_name = get_module_name(target_path)

    for line in include_lines:
        content = get_include_content(line)
        if not content: continue
        
        filename = Path(content).name
        candidates = header_map.get(filename)
        correct_path = None

        if not candidates:
            stl.append(line)
            continue
        elif len(candidates) == 1:
            correct_path = candidates[0]
        else:
            correct_path = find_exact_match(candidates, content)
            if not correct_path:
                correct_path = get_nearest_neighbor(target_path, candidates)
        
        other_mod_name = get_module_name(correct_path)
        
        if target_path.stem == correct_path.stem:
            main_header.append(line)
        elif "Engine" in correct_path.parts or "UnrealEngine" in correct_path.parts:
            engine.append(line)
        elif current_mod_name and other_mod_name and current_mod_name.lower() == other_mod_name.lower():
            same_module.append(line)
        elif "Plugins" in correct_path.parts:
            try:
                parts_lower = [p.lower() for p in correct_path.parts]
                p_idx = parts_lower.index("plugins")
                p_name = correct_path.parts[p_idx + 1]
            except: p_name = "Unknown"
            if p_name not in plugins: plugins[p_name] = []
            plugins[p_name].append(line)
        else:
            safe_key = other_mod_name if other_mod_name else "Other"
            if safe_key not in other_module: other_module[safe_key] = []
            other_module[safe_key].append(line)

    # --- 5. RECONSTRUCT ---
    new_content = []
    
    # A. Top Block (Copyright & Pragma)
    # Trim ONLY the trailing empty lines from the top block to ensure clean transition
    while other_lines_top and not other_lines_top[-1].strip():
        other_lines_top.pop()
    
    new_content.extend(other_lines_top)
    new_content.append("") # Enforce exactly one blank line before includes start
    
    # B. Headers
    if main_header: 
        new_content.extend(main_header)
        new_content.append("")
    
    if same_module:
        new_content.extend(sorted(same_module))
        new_content.append("")

    for mod in sorted(other_module.keys()):
        new_content.extend(sorted(other_module[mod]))
        new_content.append("")
        
    for plg in sorted(plugins.keys()):
        new_content.extend(sorted(plugins[plg]))
        new_content.append("")

    if engine:
        new_content.extend(sorted(engine))
        new_content.append("")
        
    if stl:
        new_content.extend(sorted(stl))
        new_content.append("")

    # C. Bottom Block
    # Remove leading empty lines from bottom to prevent double spacing
    while other_lines_bottom and not other_lines_bottom[0].strip():
        other_lines_bottom.pop(0)
        
    new_content.extend(other_lines_bottom)

    # --- 6. WRITE BACK ---
    final_text = "\n".join(new_content)
    final_text = re.sub(r'\n{3,}', '\n\n', final_text)
    final_text = final_text.rstrip() + "\n"
    
    try:
        with open(target_path, 'w', encoding=encoding, newline=original_newline) as f:
            f.write(final_text)
        print(f"   ✅ Sorted and Saved. Encoding: {encoding}, Newline: {'CRLF' if original_newline=='\r\n' else 'LF'}")
    except Exception as e:
        print(f"   ❌ Error saving file: {e}")
        
# ==========================================
# 4. ARGUMENT PARSING
# ==========================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sort C++ includes based on project modules.")
    
    # Manual files argument
    parser.add_argument("files", nargs='*', help="List of C++ files to sort")
    
    # Git flags
    parser.add_argument("--local", action="store_true", help="Process all changed files (working tree)")
    parser.add_argument("--staged", action="store_true", help="Process only staged files (index)")
    
    # MR / Diff flag
    parser.add_argument("--mr", metavar="BRANCH", help="Process files changed between HEAD and the specified branch (e.g. origin/main)")
    
    # Reindex flag
    parser.add_argument("--reindex", action="store_true", help="Force a re-scan of the project headers")
    
    args = parser.parse_args()

    files_to_process = []
    
    # 1. Collect Files based on flags
    if args.staged:
        print("--- 🌳 Detecting Staged Git Changes... ---")
        files_to_process.extend(get_git_changed_files(mode="staged"))
        
    elif args.mr:
        print(f"--- 🌳 Detecting Changes against {args.mr}... ---")
        files_to_process.extend(get_git_changed_files(mode="mr", target_branch=args.mr))

    elif args.local:
        print("--- 🌳 Detecting Local Git Changes... ---")
        files_to_process.extend(get_git_changed_files(mode="working"))
    
    if args.files:
        files_to_process.extend(args.files)

    # Remove duplicates
    files_to_process = list(set(files_to_process))

    # 2. Execution
    if files_to_process:
        index_project(force_reindex=args.reindex) 
        
        for file_path in files_to_process:
            sort_single_file(file_path)
    else:
        print("ℹ️  No files to process. Usage examples:")
        print("   python include_sorter.py file1.cpp")
        print("   python include_sorter.py --local")
        print("   python include_sorter.py --staged")
        print("   python include_sorter.py --mr origin/develop")