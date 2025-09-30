#!/usr/bin/env python3
"""
Smalipatcher: A utility tool to apply patches to smali files.
This script reads a custom .smalipatch file format and applies the specified
changes to a directory of smali code, typically extracted from an APK.
"""

import os
import sys
import re
import logging
import difflib
import argparse
from typing import List, Dict, Tuple, Optional, Literal

# Define type aliases for clarity
PatchOperation = Tuple[Literal['+', '-', ' '], str]
PatchAction = Dict[str, any]
Patch = Dict[str, any]
ApplyResult = Literal["applied", "skipped", "failed", "hunk_failed"]

def setup_logging():
    """Sets up a simple logging configuration to print messages to stdout."""
    logging.basicConfig(
        format='%(message)s',
        level=logging.INFO
    )

def apply_smalipatch(work_dir: str, patch_file: str, stop_on_fail: bool) -> bool:
    """
    Reads a .smalipatch file and applies all defined patches to the smali files
    within the specified work directory.

    Args:
        work_dir: The root directory containing the smali files.
        patch_file: The path to the .smalipatch file.
        stop_on_fail: If True, stops execution on the first failed patch.

    Returns:
        True if all patches were applied successfully or skipped, False otherwise.
    """
    try:
        with open(patch_file, 'r', encoding='utf-8') as f:
            lines = [line.rstrip('\r\n') for line in f.readlines()]
    except IOError as e:
        logging.error(f"ERROR: Could not read patch file {patch_file}: {e}")
        return False

    if not lines:
        logging.error(f"ERROR: Empty patch file {patch_file}")
        return False

    patches = parse_patches(lines)
    if not patches:
        logging.error(f"ERROR: No valid patches found in {patch_file}")
        return False

    success_count = 0
    total_patches = len(patches)
    logging.info(f"Found {total_patches} patch(es) to process.")

    for i, patch in enumerate(patches):
        logging.info("-" * 40)
        logging.info(f"Applying patch {i+1}/{total_patches} for file '{patch['file_path']}'...")
        result = apply_single_patch(work_dir, patch)
        if result == "applied":
            success_count += 1
            logging.info("✓ Patch applied successfully.")
        elif result == "skipped":
            logging.info("✓ Patch already applied or no changes needed, skipping.")
            success_count += 1
        else: # Covers "failed" and "hunk_failed"
            if stop_on_fail:
                logging.error("\nStopping execution due to patch failure (strict mode).")
                return False

    logging.info("=" * 40)
    final_message = f"Final result: {success_count}/{total_patches} patches processed successfully."
    if success_count < total_patches:
        final_message += f" ({total_patches - success_count} failed)."
    logging.info(final_message)
    return success_count == total_patches


def parse_patches(lines: List[str]) -> List[Patch]:
    """
    Parses the text lines from a .smalipatch file into a structured list of patches.

    Args:
        lines: A list of strings from the patch file.

    Returns:
        A list of patch objects, where each object represents the actions for one file.
    """
    patches: List[Patch] = []
    current_patch: Optional[Patch] = None
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        if line.startswith('FILE '):
            if current_patch:
                patches.append(current_patch)
            current_patch = {
                'file_path': line[5:].strip(),
                'actions': []
            }
            i += 1
        elif line == 'END' and current_patch:
            patches.append(current_patch)
            current_patch = None
            i += 1
        elif current_patch:
            if line.startswith('REPLACE '):
                header = line[8:].strip()
                content_lines, i = read_block_content(lines, i + 1)
                current_patch['actions'].append({
                    'type': 'REPLACE',
                    'method_sig': header,
                    'content': content_lines
                })
            elif line.startswith('PATCH '):
                header = line[6:].strip()
                operations, i = read_patch_operations(lines, i + 1)
                current_patch['actions'].append({
                    'type': 'PATCH',
                    'method_sig': header if header and not header.startswith(('+', '-')) else None,
                    'operations': operations
                })
            else:
                # Ignore empty or unrecognized lines between actions
                i += 1
        else:
            # Ignore lines before the first FILE directive
            i += 1
    
    if current_patch:
        patches.append(current_patch)

    return patches

def read_block_content(lines: List[str], start_index: int) -> Tuple[List[str], int]:
    """Reads content for an action (like REPLACE) until a terminator is found."""
    content = []
    i = start_index
    while i < len(lines):
        line_strip = lines[i].strip()
        if line_strip == 'END' or any(line_strip.startswith(kw) for kw in ['REPLACE ', 'PATCH ', 'FILE ']):
            break
        content.append(lines[i])
        i += 1
    return content, i

def read_patch_operations(lines: List[str], start_index: int) -> Tuple[List[PatchOperation], int]:
    """Reads +/-/  lines for a PATCH action."""
    operations: List[PatchOperation] = []
    i = start_index
    while i < len(lines):
        line = lines[i]
        line_strip = line.strip()
        if line_strip == 'END' or any(line_strip.startswith(kw) for kw in ['REPLACE ', 'PATCH ', 'FILE ']):
            break
        
        if line.startswith('+ '):
            operations.append(('+', line[2:]))
        elif line.startswith('- '):
            operations.append(('-', line[2:]))
        else:
            operations.append((' ', line))
        i += 1
    return operations, i


def apply_single_patch(work_dir: str, patch: Patch) -> ApplyResult:
    """
    Applies a single patch definition to its target file. This is a two-pass
    operation: first a dry run to validate changes, then the actual write.
    """
    file_path = patch['file_path']
    full_path = os.path.join(work_dir, file_path)

    if not os.path.exists(full_path):
        logging.error(f"✗ Patch Failed: Target file not found: {full_path}")
        return "failed"

    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            original_lines = [line.rstrip('\r\n') for line in f.readlines()]
    except IOError as e:
        logging.error(f"✗ Patch Failed: Could not read target file {full_path}: {e}")
        return "failed"

    # Apply changes to a copy to see if they are valid
    modified_lines = original_lines.copy()
    any_hunk_failed = False
    for action in patch['actions']:
        if action['type'] == 'REPLACE':
            result = apply_replace_action(modified_lines, action)
        elif action['type'] == 'PATCH':
            result = apply_patch_action(modified_lines, action)
        
        if result is None:
            any_hunk_failed = True
            break
        modified_lines = result

    if any_hunk_failed:
        # Specific error messages are now printed inside the apply_* functions
        return "hunk_failed"

    if original_lines == modified_lines:
        return "skipped"

    # If we are here, the patch is valid and changes are needed.
    show_diff(original_lines, modified_lines, file_path)
    
    try:
        with open(full_path, 'w', encoding='utf-8', newline='\n') as f:
            f.write('\n'.join(modified_lines) + '\n')
    except IOError as e:
        logging.error(f"✗ Patch Failed: Could not write to target file {full_path}: {e}")
        return "failed"
        
    return "applied"

def clean_smali_lines(lines: List[str]) -> Tuple[List[str], List[int]]:
    """
    Removes .line directives and empty lines from smali code for matching.

    Returns:
        A tuple containing:
        - clean_lines: List of non-empty, non-.line directive lines (stripped).
        - mapping: A list where mapping[i] is the original line index of clean_lines[i].
    """
    clean_lines = []
    mapping = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith('.line '):
            clean_lines.append(stripped)
            mapping.append(i)
    return clean_lines, mapping

def apply_replace_action(smali_lines: List[str], action: PatchAction) -> Optional[List[str]]:
    """Replaces the entire body of a method matching a signature with new content."""
    method_sig = action['method_sig']
    patch_content = action['content']
    
    pattern = method_sig_to_regex(method_sig)
    start_idx, end_idx = find_method_range(smali_lines, pattern)

    if start_idx == -1:
        logging.error(f"✗ Hunk Failed: Method for REPLACE not found: {method_sig}")
        return None
    
    new_lines = smali_lines[:start_idx + 1]
    new_lines.extend(patch_content)
    new_lines.extend(smali_lines[end_idx:])
    
    return new_lines

def apply_patch_action(smali_lines: List[str], action: PatchAction) -> Optional[List[str]]:
    """Applies a diff-style patch action (+/- lines) to the smali code."""
    operations: List[PatchOperation] = action['operations']
    method_sig: Optional[str] = action.get('method_sig')
    
    # --- Clean original and patch lines for robust matching ---
    smali_clean, smali_map = clean_smali_lines(smali_lines)
    patch_context_clean = [line.strip() for op, line in operations if op == ' ' and line.strip() and not line.strip().startswith('.line ')]
    
    if not patch_context_clean:
        logging.error("✗ Hunk Failed: PATCH action must contain at least one non-empty context line.")
        return None
        
    search_lines, search_map = smali_clean, smali_map
    search_offset = 0
    if method_sig:
        pattern = method_sig_to_regex(method_sig)
        method_start_clean, method_end_clean = find_method_range(smali_clean, pattern, is_clean=True)
        if method_start_clean == -1:
            logging.error(f"✗ Hunk Failed: Method for PATCH not found: {method_sig}")
            return None
        search_lines = smali_clean[method_start_clean : method_end_clean + 1]
        search_map = smali_map[method_start_clean : method_end_clean + 1]
        search_offset = method_start_clean

    context_start_clean = find_context(search_lines, patch_context_clean)
    
    if context_start_clean == -1:
        logging.error("✗ Hunk Failed: Patch context not found in the target file or method. The code may have changed.")
        return None

    # --- Apply modifications based on the clean match to the original lines ---
    context_start_in_smali_clean = context_start_clean + search_offset
    
    # Find the original line indices that correspond to the matched hunk
    hunk_clean_len = 0
    for op, line in operations:
        stripped = line.strip()
        if stripped and not stripped.startswith(".line "):
            hunk_clean_len += 1
    
    original_start_line = smali_map[context_start_in_smali_clean]
    
    # Find end line carefully, it might be the last line of the file
    clean_end_index = context_start_in_smali_clean + hunk_clean_len -1
    if clean_end_index >= len(smali_map):
         original_end_line = len(smali_lines)
    else:
         # Find the line *after* the hunk ends to get the slice boundary
         next_clean_line_idx = context_start_in_smali_clean + hunk_clean_len
         if next_clean_line_idx < len(smali_map):
            original_end_line = smali_map[next_clean_line_idx]
         else: # Hunk goes to the very end of the file
            original_end_line = len(smali_lines)
    
    # Construct the new lines
    new_lines = smali_lines[:original_start_line]
    new_lines.extend(line for op, line in operations if op in (' ', '+'))
    new_lines.extend(smali_lines[original_end_line:])
    
    return new_lines


def method_sig_to_regex(method_sig: str) -> re.Pattern:
    """Converts a method signature string into a compiled regex pattern for matching."""
    escaped = re.escape(method_sig)
    escaped = escaped.replace(r'\ ', r'\s+')
    return re.compile(f'^\\s*{escaped}')


def find_method_range(lines: List[str], pattern: re.Pattern, is_clean: bool = False) -> Tuple[int, int]:
    """Finds the start and end line indices of a method block."""
    end_marker = '.end method'
    for i, line in enumerate(lines):
        # If clean, line is already stripped. Otherwise, strip it.
        line_to_check = line if is_clean else line.strip()
        if pattern.match(line_to_check):
            for j in range(i + 1, len(lines)):
                line_to_check_end = lines[j] if is_clean else lines[j].strip()
                if line_to_check_end == end_marker:
                    return i, j
            return i, -1
    return -1, -1


def find_context(search_lines: List[str], context_lines: List[str]) -> int:
    """
    Finds the starting index of a sequence of context lines.
    Assumes both lists are "clean" (stripped, no .line, no empty).
    """
    if not context_lines: return -1
    for i in range(len(search_lines) - len(context_lines) + 1):
        if search_lines[i:i+len(context_lines)] == context_lines:
            return i
    return -1

def show_diff(original: List[str], modified: List[str], filename: str):
    """Displays a user-friendly, unified diff of the changes to be applied."""
    diff = difflib.unified_diff(
        original, modified, fromfile=f'a/{filename}', tofile=f'b/{filename}', lineterm=''
    )
    logging.info("--- Diff of changes to be applied ---")
    diff_lines = list(diff)
    if not diff_lines:
        logging.info("(No changes detected by diff)")
    else:
        for line in diff_lines:
            if line.startswith('+'):
                logging.info(f"\033[92m{line}\033[0m") # Green
            elif line.startswith('-'):
                logging.info(f"\033[91m{line}\033[0m") # Red
            elif line.startswith('@@'):
                logging.info(f"\033[96m{line}\033[0m") # Cyan
            else:
                logging.info(line)
    logging.info("------------------------------------")

def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description="Smalipatcher: A reverse engineering tool for manipulating apk/jar .dex files by @SameerAlSahab",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument('work_dir', help='The root directory containing the smali files.')
    parser.add_argument('patch_file', help='The path to the .smalipatch file.')
    parser.add_argument(
        '--skip-failed',
        action='store_true',
        help='Continue to the next patch even if one fails.\n(Default: Stop on the first failure).'
    )
    args = parser.parse_args()

    setup_logging()

    if not os.path.isdir(args.work_dir):
        logging.error(f"ERROR: Work directory does not exist or is not a directory: {args.work_dir}")
        sys.exit(1)

    if not os.path.isfile(args.patch_file):
        logging.error(f"ERROR: Patch file does not exist or is not a file: {args.patch_file}")
        sys.exit(1)

    # By default, stop on fail. If --skip-failed is passed, this becomes False.
    stop_on_fail = not args.skip_failed
    success = apply_smalipatch(args.work_dir, args.patch_file, stop_on_fail)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()

