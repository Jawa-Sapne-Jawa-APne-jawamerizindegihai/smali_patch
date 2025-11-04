"""
Patch Helper for Smalipatcher.

This module contains the core logic for parsing patch files and applying
the various patch actions (REPLACE, PATCH, CREATE, CREATE_METHOD).

The new PATCH logic is robust to:
- Whitespace differences
- .line, .source, #comment, and other directives
- Blank lines in the patch or target
- (With --non-strict) v#/p# register number differences
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
# Keywords that terminate a content block
BLOCK_TERMINATORS = [
    'FILE ', 'CREATE ', 'REPLACE ', 'PATCH', 'CREATE_METHOD', 'END'
]


# --- Robust Normalization Function ---

def normalize(line: str, non_strict: bool = False) -> Optional[str]:
    """
    Normalizes a smali line for loose comparison.
    Only trims whitespace and optionally generalizes registers.
    Never skips directives, comments, or .line entries.
    """
    # Remove leading/trailing whitespace and collapse inner spaces
    line = re.sub(r'\s+', ' ', line.strip())
    if line == "":
        return None  # Skip only blank lines
    
    if non_strict:
        # Replace registers like v0, v12, p1 → vX/pX, but not labels (:cond_0)
        line = re.sub(r'(?<!:)\b([vp])\d+\b', r'\1X', line)
    return line

# --- Parsing Logic (Unchanged) ---

def parse_patches(lines: List[str]) -> List[Patch]:
    """Parses text lines into a structured list of patches."""
    patches = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('FILE '):
            patch, i = parse_file_block(lines, i)
            if patch: patches.append(patch)
        elif line.startswith('CREATE '):
            patch, i = parse_create_block(lines, i)
            if patch: patches.append(patch)
        else:
            i += 1  # Skip unknown/empty lines
    return patches


def parse_file_block(lines: List[str], start_index: int) -> Tuple[Optional[Patch], int]:
    """Parses a 'FILE ... END' block."""
    file_path = lines[start_index].strip()[5:].strip()
    patch = {'type': 'FILE', 'file_path': file_path, 'actions': []}
    i = start_index + 1
    while i < len(lines):
        line = lines[i].strip()
        if line == 'END':
            return patch, i + 1
        elif line.startswith('REPLACE '):
            action, i = parse_action_block(lines, i, 'REPLACE')
            patch['actions'].append(action)
        elif line.startswith('PATCH'):  # Handles 'PATCH' and 'PATCH <sig>'
            action, i = parse_action_block(lines, i, 'PATCH')
            patch['actions'].append(action)
        elif line == 'CREATE_METHOD':
            action, i = parse_action_block(lines, i, 'CREATE_METHOD')
            patch['actions'].append(action)
        else:
            i += 1  # Skip empty/unknown lines within FILE block
    logging.warning(f"Warning: 'FILE {file_path}' block missing 'END' terminator.")
    return patch, i


def parse_create_block(lines: List[str], start_index: int) -> Tuple[Optional[Patch], int]:
    """Parses a 'CREATE ... END' block."""
    file_path = lines[start_index].strip()[7:].strip()
    content, i = read_content_block(lines, start_index + 1)
    patch = {'type': 'CREATE', 'file_path': file_path, 'content': content}
    return patch, i


def parse_action_block(lines: List[str], start_index: int, action_type: str) -> Tuple[PatchAction, int]:
    """Parses an action block (REPLACE, PATCH, CREATE_METHOD) and its content."""
    line = lines[start_index].strip()
    header = line[len(action_type):].strip()
    action = {'type': action_type}
    if header:
        action['method_sig'] = header
    
    content_lines, i = read_content_block(lines, start_index + 1)
    
    if action_type == 'PATCH':
        action['operations'] = parse_patch_operations(content_lines)
    else:
        action['content'] = content_lines
    return action, i


def read_content_block(lines: List[str], start_index: int) -> Tuple[List[str], int]:
    """Reads lines until the next action keyword or END."""
    content = []
    i = start_index
    while i < len(lines):
        line = lines[i]  # Keep original formatting
        line_strip = line.strip()
        
        is_terminator = False
        for kw in BLOCK_TERMINATORS:
             if line_strip == kw or line_strip.startswith(kw + ' '):
                 is_terminator = True
                 break
        
        if is_terminator:
            # If it's END, consume it. Otherwise, don't.
            if line_strip == 'END':
                i += 1
            return content, i
        
        content.append(line)
        i += 1
    return content, i  # Reached end of file


def parse_patch_operations(lines: List[str]) -> List[PatchOperation]:
    """Converts content lines into +/-/ operations."""
    ops = []
    for line in lines:
        if line.startswith('+ '):
            ops.append(('+', line[2:]))
        elif line.startswith('- '):
            ops.append(('-', line[2:]))
        else:
            # Keep original line, including leading whitespace, as context
            ops.append((' ', line))
    return ops


# --- File and Patch Application (Unchanged) ---

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
    """Applies all actions for a single file."""
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
            # This is the corrected function call
            result = _apply_patch(modified_lines, action, non_strict)
        elif action['type'] == 'CREATE_METHOD':
            result = _apply_create_method(modified_lines, action)
        
        if result is None:
            return "hunk_failed"  # A specific hunk failed.
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
    """Replaces the body of a method. (Unchanged)."""
    pattern = method_sig_to_regex(action['method_sig'])
    start_idx, end_idx = find_method_range(lines, pattern)

    if start_idx == -1:
        logging.error(f"✗ Hunk Failed: Method for REPLACE not found: {action['method_sig']}")
        return None
    
    return lines[:start_idx + 1] + action['content'] + lines[end_idx:]


def _apply_create_method(lines: List[str], action: PatchAction) -> Optional[List[str]]:
    """Appends a new method to the end of the file. (Unchanged)."""
    insert_pos = -1
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip().startswith('.end class'):
            insert_pos = i
            break
    
    if insert_pos == -1:
        logging.error("✗ Hunk Failed: Could not find '.end class' directive to insert method before.")
        return None
    
    new_content = action['content']
    if lines[insert_pos - 1].strip() != '':
        new_content = [''] + new_content

    logging.info(f"  -> Inserting new method before line {insert_pos + 1}")
    return lines[:insert_pos] + new_content + lines[insert_pos:]


# --- [ NEW CORRECTED PATCH FUNCTION ] ---

def _apply_patch(lines: List[str], action: PatchAction, non_strict: bool) -> Optional[List[str]]:
    """
    Applies a PATCH action with full whitespace-insensitive matching.
    - Works even if no context (' ') lines are provided.
    - Ignores blank lines and spaces.
    """
    operations = action['operations']

    # Separate operations by type
    context_ops = [(op, line) for op, line in operations if op in (' ', '-')]
    add_ops = [(op, line) for op, line in operations if op == '+']

    # If there are no context lines, apply '+'/'-' lines directly (global/simple mode)
    if not context_ops:
        logging.info("→ No context lines provided; applying add/remove globally (simple mode).")
        modified = []
        for line in lines:
            norm_line = normalize(line, non_strict)
            if not norm_line:
                modified.append(line)
                continue

            # Delete matching '-' lines if found
            if any(normalize(l, non_strict) == norm_line for _, l in operations if _ == '-'):
                continue  # skip this line (deleted)
            modified.append(line)
        # Append '+' lines at the end
        modified.extend([l for _, l in add_ops])
        return modified

    # Otherwise, use context-based hunk application
    fingerprint = [normalize(line, non_strict)
                   for op, line in context_ops
                   if normalize(line, non_strict)]

    match_start_idx = -1
    for i in range(len(lines)):
        # Match fingerprint ignoring blank lines and whitespace
        fi, li = 0, i
        while fi < len(fingerprint) and li < len(lines):
            norm_target = normalize(lines[li], non_strict)
            if norm_target is None:
                li += 1
                continue
            if norm_target == fingerprint[fi]:
                fi += 1
                li += 1
            else:
                break
        if fi == len(fingerprint):
            match_start_idx = i
            break

    if match_start_idx == -1:
        logging.error("✗ Hunk Failed: Could not find context match in target.")
        return None

    logging.info(f"  -> Context match found at line {match_start_idx+1}.")

    modified = lines[:match_start_idx]
    op_idx, li = 0, match_start_idx

    while op_idx < len(operations):
        op, patch_line = operations[op_idx]
        if op == '+':
            modified.append(patch_line)
        elif op in (' ', '-'):
            # Skip blank lines but keep structure for ' ' lines
            while li < len(lines) and normalize(lines[li], non_strict) is None:
                modified.append(lines[li])
                li += 1
            if li < len(lines):
                if op == ' ':
                    modified.append(lines[li])
                li += 1
        op_idx += 1

    modified.extend(lines[li:])
    return modified


# --- Utility Functions (Unchanged) ---

def method_sig_to_regex(method_sig: str) -> re.Pattern:
    """Converts a method signature string into a compiled regex pattern."""
    escaped = re.escape(method_sig).replace(r'\ ', r'\s+')
    return re.compile(f'^{escaped}')


def find_method_range(lines: List[str], pattern: re.Pattern) -> Tuple[int, int]:
    """Finds the start and end line indices of a method block."""
    for i, line in enumerate(lines):
        line_to_check = line.strip()
        if pattern.match(line_to_check):
            for j in range(i + 1, len(lines)):
                line_to_check_end = lines[j].strip()
                if line_to_check_end == '.end method':
                    return i, j
            return i, -1  # Found start but not end
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
                logging.info(f"\033[92m{line}\033[0m")  # Green
            elif line.startswith('-'):
                logging.info(f"\033[91m{line}\033[0m")  # Red
            elif line.startswith('@@'):
                logging.info(f"\033[96m{line}\033[0m")  # Cyan
            else:
                logging.info(line)
    logging.info("------------------------------------")
