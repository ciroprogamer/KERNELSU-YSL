# AnyKernel3 Ramdisk Mod Script
# osm0sis @ xda-developers

## AnyKernel setup
# begin properties
properties() { '
kernel.string=YSL_SukiSu Kernel(Storm Breaker based)
maintainer.string1=@HeavenRefining_DemonVenerable | @ciroprogamer86
maintainer.string2=ItsVixano TG:@GiovanniRN5
maintainer.string3=Saalim Quadri, Team StormBreaker Head

do.devicecheck=1
do.modules=0
do.systemless=1
do.cleanup=1
do.cleanuponabort=0
device.name1=ysl
supported.versions=
supported.patchlevels=
'; } # end properties

# shell variables
block=/dev/block/bootdevice/by-name/boot;
is_slot_device=0;
ramdisk_compression=auto;


## AnyKernel methods (DO NOT CHANGE)
# import patching functions/variables - see for reference
. tools/ak3-core.sh;


## AnyKernel file attributes
# set permissions/ownership for included ramdisk files
set_perm_recursive 0 0 755 644 $ramdisk/*;
set_perm_recursive 0 0 750 750 $ramdisk/init* $ramdisk/sbin;


## AnyKernel install
dump_boot;



write_boot;
## end install


#my-script

ui_print " "
ui_print "    ╔═══════════════════════════════════════════╗      "
ui_print "    ║                                           ║      "
ui_print "    ║     ⚡ YSL KERNEL - STORM BREAKER ⚡      ║      "
ui_print "    ║                                           ║      "
ui_print "    ║          SukiSU Integrated Edition        ║      "
ui_print "    ║      Modified & Enhanced by ciro          ║      "
ui_print "    ║                                           ║      "
ui_print "    ╚═══════════════════════════════════════════╝      "
ui_print " "
ui_print "   INTEGRATOR:"
ui_print "      ciro (codename: ciroprogamer86)"
ui_print "       YouTube: @HeavenRefining_DemonVenerable"
ui_print " "
ui_print "   STORMBREAKER ORIGINAL KERNEL CONTRIBUTORS:"
ui_print "     • ItsVixano | TG: @GiovanniRN5"
ui_print "     • Saalim Quadri - Team StormBreaker Head"
ui_print " "
ui_print "   DEVICE: Redmi S2/Y2 (ysl)"
ui_print " "
ui_print "──────────────────────────────────────────────────────"
ui_print "    INTEGRATED KERNEL FEATURES:"
ui_print "──────────────────────────────────────────────────────"
ui_print "  ✓ SukiSU Root Integration"
ui_print "  ✓ KPM (Kernel Patch Module)"
ui_print "  ✓ Backported set_memory.h"
ui_print "  ✓ Storm Breaker Source"
ui_print " "
ui_print "    NOTES:"
ui_print "     × SusFs not integrated (version mismatch, sorry, i refuse to backport 18446744073709551615₁₀ files)"
ui_print "     ⓘ KPM support is untested(may brick your phone if you actually use it, or may not, idk)"
ui_print " "

