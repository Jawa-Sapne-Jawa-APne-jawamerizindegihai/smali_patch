Prerequisites
This script requires Python 3 or the linux executable binary file in releases



Usage

The script is executed via the command line and requires two arguments: the working directory (root of the decompiled APK) and the patch file.

python3 smalipatch.py <work_dir> <patch_file.smalipatch>
or smalipatch <work_dir> <patch_file.smalipatch>  #exec file in releases

Example Execution
If your disassembled application files are located in apk_unpacked/ and your patch is in a subdirectory:

python3 smalipatch.py ./apk_unpacked ./patches/no-signature-check.smalipatch

Success Output:

SUCCESS: Replaced method in target/smali/com/example/PackageVerification.smali

There is a example .smalipatch file for you !!
_________________________________________________________
Check the apk_signature_disable.smalipatch and systemui_mods for deep understanding as example  
__________________________________________________________
Happy Modding ;D

USAGE in .smalipatch file

FILE <target_smali_file_path>
<ACTION_TYPE> <optional_parameters>
...patch content...
END


FILE smali/com/android/server/SystemServer.smali

REPLACE .method public static methodOne

...new method content...

.end method

END

FILE smali/com/android/server/SystemServer.smali

PATCH .method private methodTwo

    existing line 1
    
    existing line 2
    
-   line to remove
   
+   line to add
  
    existing line 3
    
END
