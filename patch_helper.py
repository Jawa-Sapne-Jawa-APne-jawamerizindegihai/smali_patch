"""
Patch Helper for Smalipatcher.

This module contains the core logic for parsing patch files and applying
the various patch actions (REPLACE, PATCH, CREATE, CREATE_METHOD).
It now includes flexible matching that uses method names and ignores spacing.
"""

import os
import re
import logging
import difflib
from typing import List, Dict, Tuple, Optional, Literal


# --- Type Aliases ---
PatchOperation = Tuple[Literal['+', '-', ' '], str]
PatchAction = Dict[str, any]
Patch = Dict[str, any]
ApplyResult = Literal["applied", "created", "skipped", "failed", "hunk_failed"]


# --- Constants ---
ACTION_KEYWORDS = ['REPLACE ', 'PATCH', 'CREATE_METHOD', 'FILE ', 'CREATE ']


# --- Parsing Logic ---

def parse_patches(lines: List[str]) -> List[Patch]:
    """
    Parses the text lines from a .smalipatch file into a structured list of patches.
    Handles FILE and CREATE top-level blocks.
    """
    patches: List[Patch] = []
    current_patch: Optional[Patch] = None
    i = 0

    while i < len(lines):
        line = lines[i].strip()
        
        if line.startswith('FILE '):
            if current_patch: patches.append(current_patch)
            current_patch = {
                'type': 'FILE',
                'file_path': line[5:].strip(),
                'actions': []
            }
            i += 1
        elif line.startswith('CREATE '):
            if current_patch: patches.append(current_patch)
            content_lines, i_after = read_block_content(lines, i + 1)
            patches.append({
                'type': 'CREATE',
                'file_path': line[7:].strip(),
                'content': content_lines
            })
            i = i_after
            current_patch = None # CREATE blocks are self-contained
        elif line == 'END' and current_patch:
            i += 1 # Ignore END, it's just for readability
        elif current_patch:
            if line.startswith('REPLACE '):
                header = line[8:].strip()
                content_lines, i = read_block_content(lines, i + 1)
                current_patch['actions'].append({
                    'type': 'REPLACE', 'method_sig': header, 'content': content_lines
                })
            elif line.strip() == 'PATCH' or line.startswith('PATCH '):
                header = line[len('PATCH'):].strip()
                operations, i = read_patch_operations(lines, i + 1)
                current_patch['actions'].append({
                    'type': 'PATCH',
                    'method_sig': header if header and not header.startswith(('+', '-')) else None,
                    'operations': operations
                })
            elif line.startswith('CREATE_METHOD'):
                content_lines, i = read_block_content(lines, i + 1)
                current_patch['actions'].append({
                    'type': 'CREATE_METHOD', 'content': content_lines
                })
            else:
                i += 1 # Ignore empty or unrecognized lines
        else:
            i += 1 # Ignore lines before the first directive

    if current_patch:
        patches.append(current_patch)

    return patches


def read_block_content(lines: List[str], start_index: int) -> Tuple[List[str], int]:
    """Reads content for an action until a terminator or the next action keyword is found."""
    content = []
    i = start_index
    while i < len(lines):
        line_strip = lines[i].strip()

        is_keyword = False
        for kw in ACTION_KEYWORDS:
            kw_no_space = kw.strip()
            if line_strip == kw_no_space or line_strip.startswith(kw.strip() + ' '):
                is_keyword = True
                break
        
        if line_strip == 'END' or is_keyword:
            break
        content.append(lines[i])
        i += 1
    if i < len(lines) and lines[i].strip() == 'END':
        i += 1
    return content, i


def read_patch_operations(lines: List[str], start_index: int) -> Tuple[List[PatchOperation], int]:
    """Reads +/-/ lines for a PATCH action until a terminator or the next action."""
    operations: List[PatchOperation] = []
    i = start_index
    while i < len(lines):
        line = lines[i]
        line_strip = line.strip()

        is_keyword = False
        for kw in ACTION_KEYWORDS:
            kw_no_space = kw.strip()
            if line_strip == kw_no_space or line_strip.startswith(kw.strip() + ' '):
                is_keyword = True
                break

        if line_strip == 'END' or is_keyword:
            break
        
        if line.startswith('+ '):
            operations.append(('+', line[2:]))
        elif line.startswith('- '):
            operations.append(('-', line[2:]))
        else:
            operations.append((' ', line))
        i += 1
    if i < len(lines) and lines[i].strip() == 'END':
        i += 1
    return operations, i


# --- File and Patch Application ---

def apply_create_action(work_dir: str, patch: Patch) -> ApplyResult:
    """Creates a new smali file."""
    file_path = patch['file_path']
    full_path = os.path.join(work_dir, file_path)

    if os.path.exists(full_path):
        logging.warning(f"~ Skipped: File to create already exists: {full_path}")
        return "skipped"

    try:
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, 'w', encoding='utf-8', newline='\n') as f:
            f.write('\n'.join(patch['content']) + '\n')
        logging.info(f"  -> Created new file: {file_path}")
        return "created"
    except IOError as e:
        logging.error(f"✗ Create Failed: Could not write to target file {full_path}: {e}")
        return "failed"


def apply_file_patch(work_dir: str, patch: Patch, non_strict: bool) -> ApplyResult:
    """
    Applies all actions for a single file. If any action fails, the entire
    file is considered failed, and original content is restored if necessary.
    """
    full_path = os.path.join(work_dir, patch['file_path'])

    if not os.path.exists(full_path):
        logging.error(f"✗ Patch Failed: Target file not found: {full_path}")
        return "failed"

    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            original_lines = [line.rstrip('\r\n') for line in f.readlines()]
    except IOError as e:
        logging.error(f"✗ Patch Failed: Could not read target file {full_path}: {e}")
        return "failed"

    modified_lines = original_lines[:]
    
    for j, action in enumerate(patch['actions']):
        action_type = action.get('type', 'Unknown')
        logging.info(f"--> Applying action {j+1}/{len(patch['actions'])} ({action_type})...")
        
        result = None
        if action['type'] == 'REPLACE':
            result = _apply_replace(modified_lines, action)
        elif action['type'] == 'PATCH':
            result = _apply_patch_flexible(modified_lines, action)
        elif action['type'] == 'CREATE_METHOD':
            result = _apply_create_method(modified_lines, action)
        
        if result is None:
            return "hunk_failed" # A specific hunk failed.
        modified_lines = result

    if original_lines == modified_lines:
        return "skipped"

    # All actions were valid, and changes were made.
    show_diff(original_lines, modified_lines, patch['file_path'])
    
    try:
        with open(full_path, 'w', encoding='utf-8', newline='\n') as f:
            f.write('\n'.join(modified_lines) + '\n')
    except IOError as e:
        logging.error(f"✗ Patch Failed: Could not write to target file {full_path}: {e}")
        return "failed"
        
    return "applied"


# --- Specific Action Implementations ---

def _apply_replace(lines: List[str], action: PatchAction) -> Optional[List[str]]:
    """Replaces the body of a method."""
    pattern = method_sig_to_regex(action['method_sig'])
    start_idx, end_idx = find_method_range(lines, pattern)

    if start_idx == -1:
        logging.error(f"✗ Hunk Failed: Method for REPLACE not found: {action['method_sig']}")
        return None
    
    return lines[:start_idx + 1] + action['content'] + lines[end_idx:]


def _apply_create_method(lines: List[str], action: PatchAction) -> Optional[List[str]]:
    """Appends a new method to the end of the file."""
    # Find the last occurrence of '.end method' to insert after,
    # or the '.class' line to insert before if no methods exist.
    insert_pos = -1
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip().startswith('.end class'):
            insert_pos = i
            break
    
    if insert_pos == -1:
        logging.error("✗ Hunk Failed: Could not find '.end class' directive to insert method before.")
        return None
    
    # Add an extra newline for spacing if the preceding line isn't empty
    new_content = action['content']
    if lines[insert_pos - 1].strip() != '':
        new_content = [''] + new_content

    logging.info(f"  -> Inserting new method before line {insert_pos + 1}")
    return lines[:insert_pos] + new_content + lines[insert_pos:]


def _apply_patch_flexible(lines: List[str], action: PatchAction) -> Optional[List[str]]:
    """
    NEW FLEXIBLE PATCH: Searches for method by name, then applies modifications
    ignoring spacing and focusing on content.
    """
    operations = action['operations']
    method_sig = action.get('method_sig')
    
    # Extract the search pattern from the first context line
    search_pattern = None
    for op, line in operations:
        if op == ' ' and line.strip():
            search_pattern = line.strip()
            break
    
    if not search_pattern:
        logging.error("✗ Hunk Failed: No search pattern found in PATCH operations")
        return None
    
    logging.info(f"  -> Searching for pattern: '{search_pattern}'")
    
    # Find all potential locations
    potential_starts = []
    for i, line in enumerate(lines):
        if search_pattern in line:
            potential_starts.append(i)
    
    if not potential_starts:
        logging.error(f"✗ Hunk Failed: Search pattern '{search_pattern}' not found in file")
        return None
    
    # Try each potential location
    for start_idx in potential_starts:
        result = _try_apply_at_location(lines, start_idx, operations)
        if result is not None:
            logging.info(f"  -> Successfully applied at line {start_idx + 1}")
            return result
    
    logging.error("✗ Hunk Failed: Could not apply patch at any matching location")
    return None


def _try_apply_at_location(lines: List[str], start_idx: int, operations: List[PatchOperation]) -> Optional[List[str]]:
    """
    Try to apply patch operations starting at the given location.
    Returns modified lines if successful, None if failed.
    """
    current_idx = start_idx
    modified_lines = lines[:]
    ops_applied = 0
    
    for op, content in operations:
        content_stripped = content.strip()
        
        if op == ' ':  # Context line - must match
            if current_idx >= len(modified_lines):
                return None
            current_line_stripped = modified_lines[current_idx].strip()
            if content_stripped != current_line_stripped:
                return None
            current_idx += 1
            ops_applied += 1
            
        elif op == '-':  # Delete line
            if current_idx >= len(modified_lines):
                return None
            current_line_stripped = modified_lines[current_idx].strip()
            if content_stripped != current_line_stripped:
                return None
            # Remove the line
            modified_lines = modified_lines[:current_idx] + modified_lines[current_idx + 1:]
            ops_applied += 1
            # Don't increment current_idx because we removed a line
            
        elif op == '+':  # Add line
            modified_lines = modified_lines[:current_idx] + [content] + modified_lines[current_idx:]
            current_idx += 1
            ops_applied += 1
    
    # If we applied at least one operation, consider it successful
    if ops_applied > 0:
        return modified_lines
    
    return None


# --- Utility Functions ---

def clean_and_map_lines(lines: List[str]) -> Tuple[List[str], List[int], int]:
    """
    Removes comments, .line directives, and empty lines for matching.
    Returns (clean_lines, mapping_to_original_indices, offset_always_zero).
    """
    clean_lines = []
    mapping = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith(('.line ', '#', '.source ')):
            clean_lines.append(stripped)
            mapping.append(i)
    return clean_lines, mapping, 0


def method_sig_to_regex(method_sig: str) -> re.Pattern:
    """Converts a method signature string into a compiled regex pattern."""
    escaped = re.escape(method_sig).replace(r'\ ', r'\s+')
    return re.compile(f'^{escaped}')


def find_method_range(lines: List[str], pattern: re.Pattern, is_clean: bool = False) -> Tuple[int, int]:
    """Finds the start and end line indices of a method block."""
    for i, line in enumerate(lines):
        line_to_check = line if is_clean else line.strip()
        if pattern.match(line_to_check):
            for j in range(i + 1, len(lines)):
                line_to_check_end = lines[j] if is_clean else lines[j].strip()
                if line_to_check_end == '.end method':
                    return i, j
            return i, -1 # Found start but not end
    return -1, -1


def show_diff(original: List[str], modified: List[str], filename: str):
    """Displays a user-friendly, unified diff of the changes."""
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
