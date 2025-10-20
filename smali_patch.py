#!/usr/bin/env python3
"""
Smalipatcher: A utility tool to apply patches to smali files.

This script reads a custom .smalipatch file format and applies the specified
changes to a directory of smali code.

This updated version uses a more robust patch helper that ignores
directives, whitespace, and (optionally) register numbers.
"""

import os
import sys
import logging
import argparse
from typing import List

# Import core logic from the helper script
import patch_helper

def setup_logging():
    """Sets up a simple logging configuration to print messages to stdout."""
    # Add color support for Windows
    if os.name == 'nt':
        try:
            import colorama
            colorama.init()
        except ImportError:
            pass
            
    logging.basicConfig(
        format='%(message)s',
        level=logging.INFO
    )

def apply_smalipatch(work_dir: str, patch_file: str, stop_on_fail: bool, non_strict: bool) -> bool:
    """
    Reads a .smalipatch file and applies all defined patches to the smali files
    within the specified work directory.

    Args:
        work_dir: The root directory containing the smali files.
        patch_file: The path to the .smalipatch file.
        stop_on_fail: If True, stops execution on the first failed patch.
        non_strict: If True, ignores v/p register number differences during matching.

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

    patches = patch_helper.parse_patches(lines)
    if not patches:
        logging.error(f"ERROR: No valid patch definitions found in {patch_file}")
        return False

    success_count = 0
    total_patches = len(patches)
    logging.info(f"Found {total_patches} patch definition(s).")
    if non_strict:
        logging.info("Running in NON-STRICT mode: v/p register numbers will be ignored.")

    for i, patch in enumerate(patches):
        logging.info("-" * 40)
        patch_type = patch.get('type', 'FILE')
        
        if patch_type == 'CREATE':
            logging.info(f"Processing CREATE {i+1}/{total_patches}: '{patch['file_path']}'...")
            result = patch_helper.apply_create_action(work_dir, patch)
        else: # This handles FILE patches
            logging.info(f"Processing FILE {i+1}/{total_patches}: '{patch['file_path']}' ({len(patch['actions'])} action(s))...")
            result = patch_helper.apply_file_patch(work_dir, patch, non_strict)

        if result in ("applied", "created"):
            success_count += 1
            logging.info(f"✓ {patch_type} action applied successfully.")
        elif result == "skipped":
            logging.info("✓ No changes needed or already applied, skipping.")
            success_count += 1
        else: # Covers "failed" and "hunk_failed"
            if stop_on_fail:
                logging.error("\nStopping execution due to patch failure (strict mode).")
                return False

    logging.info("=" * 40)
    final_message = f"Final result: {success_count}/{total_patches} definition(s) processed successfully."
    if success_count < total_patches:
        final_message += f" ({total_patches - success_count} failed)."
    logging.info(final_message)
    return success_count == total_patches

def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description="Smalipatcher: A reverse engineering tool for manipulating apk/jar .dex files.",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""
Patch file commands:
  FILE <path/to/file.smali>
    REPLACE <method_signature>
      ... new method body ...
    END
    PATCH <optional_method_signature>
      ... diff-style +/- lines ...
    END
    CREATE_METHOD
      ... full new method body ...
    END
  END

  CREATE <path/to/new_file.smali>
    ... full new file content ...
  END
"""
    )
    parser.add_argument('work_dir', help='The root directory containing the smali files.')
    parser.add_argument('patch_file', help='The path to the .smalipatch file.')
    parser.add_argument(
        '--skip-failed',
        action='store_true',
        help='Continue to the next patch even if one fails.\n(Default: Stop on the first failure).'
    )
    parser.add_argument(
        '--non-strict',
        action='store_true',
        help='Ignore v#/p# register number differences when matching PATCH context.\nThis can help apply patches to slightly modified code.'
    )
    args = parser.parse_args()

    setup_logging()

    if not os.path.isdir(args.work_dir):
        logging.error(f"ERROR: Work directory does not exist or is not a directory: {args.work_dir}")
        sys.exit(1)

    if not os.path.isfile(args.patch_file):
        logging.error(f"ERROR: Patch file does not exist or is not a file: {args.patch_file}")
        sys.exit(1)

    stop_on_fail = not args.skip_failed
    success = apply_smalipatch(args.work_dir, args.patch_file, stop_on_fail, args.non_strict)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
