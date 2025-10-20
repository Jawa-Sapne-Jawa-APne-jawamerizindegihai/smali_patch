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
    Normalizes a smali line for robust comparison.

    Returns:
        A normalized string, or None if the line should be skipped entirely.
    """
    line = line.strip()
    
    # 1. Ignore comments, directives, and empty lines
    if not line or line.startswith(('.line', '#', '.source', '.prologue', '.epilogue')):
        return None  # This line is "skippable"
        
    # 2. Collapse all internal whitespace to a single space
    line = re.sub(r'\s+', ' ', line)
    
    # 3. Handle non-strict register matching
    if non_strict:
        # Replaces v1, v22, p0, p10 etc. with a generic 'vX' or 'pX'
        line = re.sub(r'\b([vp])\d+\b', r'\1X', line)
    
    return line


# --- New Parsing Logic ---

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
    """Replaces the body of a method. (Unchanged from original)."""
    pattern = method_sig_to_regex(action['method_sig'])
    start_idx, end_idx = find_method_range(lines, pattern)

    if start_idx == -1:
        logging.error(f"✗ Hunk Failed: Method for REPLACE not found: {action['method_sig']}")
        return None
    
    return lines[:start_idx + 1] + action['content'] + lines[end_idx:]


def _apply_create_method(lines: List[str], action: PatchAction) -> Optional[List[str]]:
    """Appends a new method to the end of the file. (Unchanged from original)."""
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


def _apply_patch(lines: List[str], action: PatchAction, non_strict: bool) -> Optional[List[str]]:
    """
    Applies a PATCH action using a robust, line-by-line streaming match.
    This is the new, robust logic.
    """
    
    operations = action['operations']
    
    target_idx = 0       # Current line in target file
    op_idx = 0           # Current operation in patch
    
    modified_lines = []  # The new list of lines we are building
    
    while op_idx < len(operations):
        op, patch_line = operations[op_idx]
        
        if op == '+':
            # ADD: Just add the line to our new list.
            modified_lines.append(patch_line)
            op_idx += 1
            continue
        
        # If we are here, op is ' ' or '-'.
        # We must find a matching line in the target.
        
        # 1. Get the next meaningful patch line
        norm_patch_line = normalize(patch_line, non_strict)
        
        # Skip this operation if it's not meaningful (e.g., a comment, .line)
        if norm_patch_line is None:
            op_idx += 1
            continue
        
        # 2. Get the next meaningful target line
        norm_target_line = None
        while target_idx < len(lines):
            norm_target_line = normalize(lines[target_idx], non_strict)
            if norm_target_line is not None:
                break  # Found a meaningful (non-directive) target line
            
            # It was a skippable line, so add it to output and keep scanning
            modified_lines.append(lines[target_idx])
            target_idx += 1
        
        if target_idx == len(lines):
            # We ran out of target lines to match against
            logging.error(f"✗ Hunk Failed: Reached end of file while searching for: '{patch_line.strip()}'")
            return None

        # 3. We have two meaningful, normalized lines. Compare them.
        if norm_patch_line != norm_target_line:
            logging.error(f"✗ Hunk Failed: Mismatch.")
            logging.error(f"  Expected (normalized): '{norm_patch_line}' (from '{patch_line.strip()}')")
            logging.error(f"  Got (normalized):      '{norm_target_line}' (from '{lines[target_idx].strip()}' at line {target_idx + 1})")
            return None
            
        # 4. They match!
        if op == ' ':
            # CONTEXT: Add the *original* target line to the output
            modified_lines.append(lines[target_idx])
            target_idx += 1
            op_idx += 1
        elif op == '-':
            # DELETE: *Don't* add the target line.
            # Just advance both pointers.
            target_idx += 1
            op_idx += 1
            
    # We've applied all patch operations.
    # Now, append any remaining lines from the original file.
    modified_lines.extend(lines[target_idx:])
    
    return modified_lines


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
