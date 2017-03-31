# Copyright 2014 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Classes to interact with android emulator."""




import collections
import contextlib
import logging
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import telnetlib
import tempfile
import threading
import time


# pylint: disable=g-import-not-at-top
try:
  from M2Crypto import X509
except ImportError as e:
  # Initialize X509 to None, so that we don't get NameErrors while trying to
  # boot up a device if users have not installed the X509 library.
  X509 = collections.namedtuple('X509', ['FORMAT_PEM', 'FORMAT_DER'])
  print ('If you want to add certificates to the emulator, Please install '
         'MCrypt library using sudo apt-get install python-mcrypt .')
import portpicker
import gflags as flags
import gflags as flags_validators
from tools.android.emulator import resources
from google.apputils import stopwatch
from tools.android.emulator import common
from tools.android.emulator import emulator_meta_data_pb2

from tools.android.emulator import xserver

FLAGS = flags.FLAGS
flags.DEFINE_integer('data_partition_size', None, '[START ONLY] expand data '
                     'partition to a bigger size. The unit is in megabytes.')
flags.DEFINE_integer('long_press_timeout', None, 'Timeout for considering '
                     'press to be long.The unit is in millisecond.')
flags.DEFINE_bool('hardware_keyboard', True, 'Whether to connect hardware '
                  'keyboard to device.')
flags.DEFINE_bool('boost_dex2oat', False, 'Decrease dex2oat time. This could '
                  'hurt runtime performance.')
flags.DEFINE_integer('cores', 2, 'Cores number for emulated devices, only '
                     'meaningful for qemu2.')
flags.DEFINE_bool('skip_connect_device', False, 'Skip to connect device.')

LoadInfo = collections.namedtuple('LoadInfo', 'timestamp up_time idle_time')

Properties = collections.namedtuple('Properties', 'name value')

READ_BUFFER_SIZE = 1024 * 1024  # 1MB.

# speeds from:
# http://developer.android.com/guide/developing/devices/emulator.html#netspeed
# and
# http://chimera.labs.oreilly.com/books/1230000000545/ch07.html

NET_TYPE_TO_SPEED = {
    'edge': '118.4:236.8',
    'fastnet': 'full',
    'gprs': '40.0:80.0',
    'gsm': '14.4:14.4',
    'hscsd': '14.4:43.2',
    'hsdpa': '348.0:14400.0',
    'umts': '128.0:1920.0'
}

NET_TYPE_TO_DELAY = {
    'edge': '80:400',
    'fastnet': 'none',
    'gprs': '150:550',
    'gsm': '300:1000',
    'hscsd': 'none',  # no data
    'hsdpa': 'none',  # no data
    'umts': '35:200'
}

# HONEYCOMB has a bug where netcfg dhcp eth0
# will kill the adb connection permenantly
# luckily it does not need to dhcp on boot.
HONEYCOMB_SYSTEM_IMAGES = [
    'google_inc_13',
    'google_13',
    'android_13']

PERMANENT_INSTALL_ERROR = [
    'INSTALL_FAILED_INVALID_APK',
    'INSTALL_FAILED_OLDER_SDK',
    'INSTALL_FAILED_INSUFFICIENT_STORAGE',
    'INSTALL_FAILED_NO_MATCHING_ABIS',
    'INSTALL_FAILED_VERSION_DOWNGRADE',
    'INSTALL_FAILED_PERMISSION_MODEL_DOWNGRADE',
    'INSTALL_PARSE_FAILED_MANIFEST_MALFORMED']

# These services are not stopped at shutdown time.
# We depend on them to communicate with emulated devices.
SHUTDOWN_PROTECTED_SERVICES = ['pipe_traverse', 'adbd']

GUEST_OPEN_GL = 'guest'
HOST_OPEN_GL = 'host'
MESA_OPEN_GL = 'mesa'
NO_OPEN_GL = 'no_open_gl'
SWIFTSHADER_OPEN_GL = 'swiftshader'
OPEN_GL_DRIVERS = [MESA_OPEN_GL, HOST_OPEN_GL, NO_OPEN_GL, GUEST_OPEN_GL,
                   SWIFTSHADER_OPEN_GL]

SDCARD_SIZE_KEY = 'sdcard_size_mb'

SYSTEM_ABI_KEY = 'systemimage.abi'
API_LEVEL_KEY = 'androidversion.apilevel'
HEAP_GROWTH_LIMIT_KEY = 'dalvik.vm.heapgrowthlimit'

EMULATOR_TYPE_KEY = 'ro.mobile_ninjas.emulator_type'

SENSITIVE_SYSTEM_IMAGE = 'sensitive.systemimage'

ADB_INSTALL_TIMEOUT_SECONDS = 90
ADB_SHORT_TIMEOUT_SECONDS = 20

_OPEN_TARBALL = 'OPEN_TARBALL'
_EXTRACT_TARBALL = 'EXTRACT_TARBALL'
_BOOT_COMPLETE_PRESENT = 'CHECK_BOOT_PROP'
_BOOT_COMPLETE_FAIL_SLEEP = 'CHECK_BOOT_PROP_FAIL_SLEEP'
_LAUNCHER_STARTED = 'CHECK_BOOT_PROP'
_LAUNCHER_STARTED_FAIL_SLEEP = 'CHECK_BOOT_PROP_FAIL_SLEEP'
_PIPE_TRAVERSAL_CHECK = 'PIPE_TRAVERSAL_CHECK'
_PIPE_TRAVERSAL_CHECK_FAIL_SLEEP = 'PIPE_TRAVERSAL_CHECK_FAIL_SLEEP'
_SNAPSHOT_COPY = 'SNAPSHOT_COPY'
_ADB_LISTENING_CHECK = 'ADB_LISTENING_CHECK'
_ADB_LISTENING_CHECK_FAIL_SLEEP = 'ADB_LISTENING_CHECK_FAIL_SLEEP'
_SDCARD_CREATE = 'SDCARD_CREATE'
_STAGE_DATA = 'STAGE_DATA'
_START_PROCESS = 'START_PROCESS'
_KILL_EMULATOR = 'KILL_EMULATOR'
_SPAWN_EMULATOR = 'SPAWN_EMULATOR'
_ADB_CONNECT = 'ADB_CONNECT'
_ADB_CONNECT_FAIL_SLEEP = 'ADB_CONNECT_FAIL_SLEEP'
_SYS_SERVER_CHECK = 'SYS_SERVER_CHECK'
_SYS_SERVER_CHECK_FAIL_SLEEP = 'SYS_SERVER_CHECK_FAIL_SLEEP'
_PM_CHECK = 'PM_CHECK'
_PM_CHECK_FAIL_SLEEP = 'PM_CHECK_FAIL_SLEEP'
_ENSURE_CACHED = 'ENSURE_CACHED'
_INSTALL_TASK = 'INSTALL_TASK'
_SD_CARD_MOUNT_CHECK = 'SD_CARD_MOUNT_CHECK'
_SD_CARD_MOUNT_CHECK_FAIL_SLEEP = 'SD_CARD_MOUNT_CHECK_FAIL_SLEEP'
_RAMDISK_MOD = 'RAMDISK_MOD'
_CHECK_DPI = 'CHECK_DPI'
_CHECK_DPI_FAIL_SLEEP = 'CHECK_DPI_FAIL_SLEEP'

_ANR_RE = re.compile(r'\w\/(am_(?:crash|anr|proc_died)).*\[(\w.*)\]')
_DEV_NULL = open('/dev/null')

_DENSITY_TVDPI = 213

# A quick way to get this list:
# Generate an avd which api level 19+
# ~/Android/Sdk/tools/android create avd -n K -t 16 --abi x86
# Run
# ~/Android/Sdk/tools/emulator -avd K -qemu -lcd-density 1
# It will tell you in error message.
# TODO: automatically update this list so we don't need to maintain it
# manually.
_BUCKET_DPI = [120, 160, 213, 240, 280, 320, 360, 400, 420, 480, 560, 640]
# Make sure it's sorted.
_BUCKET_DPI.sort()
# tvdpi is not part of the dpi bucket list
_BUCKET_DPI.remove(_DENSITY_TVDPI)

_DB_PATH = '/data/data/com.android.providers.settings/databases/settings.db'

_DEFAULT_QEMU_TELNET_PORT = 52222

_BOOTSTRAP_PKG = 'com.google.android.apps.common.testing.services.bootstrap'
_BOOTSTRAP_PATH = 'android_test_support/tools/android/emulator/daemon/bootstrap.apk'

_DEFAULT_BROADCAST_ACTION = 'ACTION_MOBILE_NINJAS_START'

_CONSOLE_TOKEN_DEVICE_PATH = '/data/console_token'

_MAX_CORES_NUM = 16


@flags.Validator('cores')
def _CheckCoresFlag(cores):
  """Check cores flag value for validity."""
  if cores < 1 or cores > _MAX_CORES_NUM:
    raise flags_validators.Error(
        '%d not a valid cores number[1 - %d]' % (cores, _MAX_CORES_NUM))
  return True


def _IPv6OnlyEnv():
  """Check if system only provides IPv6 connectivity."""
  working = set()
  for af in [socket.AF_INET, socket.AF_INET6]:
    try:
      s = socket.socket(af, socket.SOCK_STREAM)
      s.close()
      working.add(af)
    except socket.error:
      pass
  return socket.AF_INET6 in working and socket.AF_INET not in working


class AndroidPlatform(object):
  """Used to find all the binaries offered in the android sdk."""

  def __init__(self, android_sdk='third_party/java/android/android_sdk_linux'):
    self.android_sdk = os.path.join('android_test_support', android_sdk)
    self._android_platform_tools = os.path.join(self.android_sdk,
                                                'platform-tools')
    self.adb = None
    self.emulator_x86 = None
    self.emulator_arm = None
    self.emulator_wrapper_launcher = None
    self.empty_snapshot_fs = None
    self.mksdcard = None
    self.prepended_library_path = ''  # prepended to the emulators lib path
    self.base_emulator_path = ''  # emulator binaries directory
    self.emulator_support_lib_path = ''  # shared libs the emu needs
    self.xkb_path = ''  # xkb stuff X/Qt need.
    self.kvm_device = '/dev/kvm'
    self.bios_files = None
    self.bios_dir = None
    self.real_adb = None

  def GetEmulator(self, arch_type, emulator_type):
    """Gets the Emulator Launcher based on the architecture."""
    logging.info('Emulator type: %d', emulator_type)

    if emulator_type == emulator_meta_data_pb2.EmulatorMetaDataPb.QEMU2:
      return self.emulator_wrapper_launcher

    if 'x86' == arch_type:
      return self.emulator_x86
    elif arch_type.startswith('arm'):
      return self.emulator_arm
    else:
      raise Exception('Unknown arch: %s' % arch_type)

  def MakeBiosDir(self, tmp_dir):
    """Creates a temp directory to hold bios files."""
    if not self.bios_dir:
      self.bios_dir = tmp_dir
      if self.bios_files:
        for bios_file in self.bios_files:
          shutil.copy2(bios_file, self.bios_dir)
      else:
        emulator_dir = os.path.dirname(self.emulator_x86)
        lib_dir = os.path.join(emulator_dir, 'lib', 'pc-bios')
        for root, unused_subdirs, files in os.walk(lib_dir):
          for filepath in files:
            shutil.copy2(os.path.join(root, filepath),
                         self.bios_dir)
    return self.bios_dir


default_android_platform = AndroidPlatform()


class EmulatedDevice(object):
  """An interface to manage emulated android devices."""

  def __init__(self, adb_server_port=None, emulator_telnet_port=None,
               emulator_adb_port=None, device_serial=None,
               android_platform=None, qemu_gdb_port=0,
               enable_single_step=False,
               logcat_path=None, logcat_filter='*:D',
               enable_console_auth=False,
               enable_g3_monitor=True,
               enable_gps=True,
               add_insecure_cert=False):
    self.adb_server_port = adb_server_port
    self.emulator_adb_port = emulator_adb_port
    self.emulator_telnet_port = emulator_telnet_port
    self.device_serial = device_serial
    self.android_platform = android_platform
    self._metadata_pb = None
    self._connect_poll_interval = 1
    self._connect_max_attempts = 300
    self._emulator_log_file = None
    self._images_dir = None
    self._running = False
    self._kicked_launcher = False
    self._emulator_start_args = None
    self._emulator_env = None
    self._emu_process_pid = None
    self._sysimages_tmp_dir = None
    self._qemu_gdb_port = qemu_gdb_port
    self._enable_single_step = enable_single_step
    self._emulator_tmp_dir = None
    self.delete_temp_on_exit = True
    self._child_will_delete_tmp = True
    self._sockets_dir = None
    self._pipe_traversal_log_dir = None
    self._pipe_traversal_running = None
    self._vm_running = True
    self._logcat_path = logcat_path
    self._logcat_filter = logcat_filter
    self._enable_console_auth = enable_console_auth
    self._console_auth_token_file = None
    self._enable_g3_monitor = enable_g3_monitor
    self._enable_gps = enable_gps
    self._add_insecure_cert = add_insecure_cert
    # There is a hard coded 10 minutes timeout in bazel side.
    # We use a shorter cut off here to make sure we have chances
    # to print log.
    self._time_out_time = time.time() + 580
    self._use_real_adb = False

  def _IsUserBuild(self, build_prop):
    """Check if a build is user build from build.prop file."""

    with open(build_prop, 'r') as f:
      return 'ro.build.type=user\n' in f.read()
    return False

  def _IsPipeTraversalRunning(self):
    if self._pipe_traversal_running is None:
      if self._SnapshotPresent().value == 'True':
        # snapshot restore - it must be started explicitly.
        self._pipe_traversal_running = False
      else:
        # fresh boot - of course it's running.
        self._pipe_traversal_running = True
    return self._pipe_traversal_running

  def PreverifyApks(self):
    """Causes APKs to be verified upon installation."""
    logging.info('enabling preverify...')
    # no longer applicable in ART world.
    if self.GetApiVersion() <= 20:
      #  v=a,o=v means -Xverify:all -Xdexopt:verified
      self.ExecOnDevice(['setprop', 'dalvik.vm.dexopt-flags', 'v=a,o=v'])
      self._RestartAndroid()
      self._PollEmulatorStatus()
      self._UnlockScreen()

  def _PossibleImgSuffix(self):
    if (self._metadata_pb.emulator_type ==
        emulator_meta_data_pb2.EmulatorMetaDataPb.QEMU2):
      return '.qcow2'
    return ''

  def _UserdataQemuFile(self):
    return os.path.join(self._SessionImagesDir(), 'userdata-qemu.img')

  def _SnapshotFile(self):
    return os.path.join(self._SessionImagesDir(), 'snapshots.img')

  def _SdcardFile(self):
    return os.path.join(self._SessionImagesDir(), 'sdcard.img')

  def _CacheFile(self):
    return os.path.join(self._SessionImagesDir(), 'cache.img')

  def _RamdiskFile(self):
    return os.path.join(self._SessionImagesDir(), 'ramdisk.img')

  def _InitSystemFile(self):
    return os.path.join(self._InitImagesDir(), 'system.img')

  def _SystemName(self):
    if (self._metadata_pb.emulator_type ==
        emulator_meta_data_pb2.EmulatorMetaDataPb.QEMU2):
      return 'system.img'
    return 'system-qemu.img'

  def _SystemFile(self):
    return os.path.join(self._SessionImagesDir(), self._SystemName())

  def _KernelFileName(self):
    if (self._metadata_pb.emulator_type ==
        emulator_meta_data_pb2.EmulatorMetaDataPb.QEMU2):
      return 'kernel-ranchu'
    return 'kernel-qemu'

  def _KernelFile(self):
    return os.path.join(self._InitImagesDir(), self._KernelFileName())

  def _InitImagesDir(self):
    return os.path.join(self._images_dir, 'init')

  def _SessionImagesDir(self):
    return os.path.join(self._images_dir, 'session')

  def _SparseCp(self, src, dst):
    """Copies a file and respects its sparseness.

    Symbolic links are dereferenced.

    Args:
      src: the source file
      dst: the destination file
    """
    subprocess.check_call(
        ['cp', '--sparse=always', '--dereference', src, dst])

  def _ExtractTarEntry(self, archive, entry, working_dir):
    """Extracts a single entry from a compressed tar archive."""
    subprocess.check_call([
        'tar', '-xzSf', archive, '-C', working_dir, '--no-anchored', entry])

  def _StageDataFiles(self, system_image_dir, system_image_path,
                      userdata_tarball, timer, enable_guest_gl):
    """Stages files for the emulator launch."""

    self._images_dir = os.path.abspath(self._TempDir('images'))
    os.makedirs(self._InitImagesDir())
    os.makedirs(self._SessionImagesDir())

    # kernel is never compressed (thank god.)
    init_kernel = os.path.abspath(
        os.path.join(system_image_dir, self._KernelFileName()))
    assert os.path.exists(init_kernel)
    os.symlink(init_kernel, self._KernelFile())

    init_sys = os.path.abspath(system_image_path)
    assert os.path.exists(init_sys), '%s: no system.img' % system_image_path
    if system_image_path.endswith('.img'):
      os.symlink(init_sys, self._InitSystemFile())
      self._SparseCp(self._InitSystemFile(), self._SystemFile())
    else:
      assert system_image_path.endswith('.img.tar.gz'), 'Not known format'
      self._ExtractTarEntry(
          init_sys, 'system.img', os.path.dirname(self._SystemFile()))
      shutil.move(os.path.join(os.path.dirname(self._SystemFile()),
                               'system.img'),
                  self._SystemFile())

    use_ext4 = 'ext4' in subprocess.check_output(['file', self._SystemFile()])
    if use_ext4:
      # We can only alter ext2|3|4 image now.
      # hwcomposer caused some issues for us in the past. So we disabled it.
      # But new API level image can't work without this.
      # TODO: remove this hack for all API levels after we fix issues
      # on all API levels.
      debugfs_cmd = []
      if self.GetApiVersion() < 25:
        debugfs_cmd += ['rm /lib/hw/hwcomposer.goldfish.so',
                        'rm /lib/hw/hwcomposer.ranchu.so']
      if self.GetApiVersion() < 24:
        if not enable_guest_gl:
          # Delete egl libraries in system image.
          debugfs_cmd += ['unlink /vendor/lib/egl']
      elif self._add_insecure_cert:
        debugfs_cmd += self.GetInstallCertCmd()
      build_prop = os.path.join(self._images_dir, 'build.prop')
      debugfs_cmd += ['dump /build.prop %s' % build_prop]
      self.ExecDebugfsCmd(self._SystemFile(), debugfs_cmd)
      # Pipe service won't work for user build and api level 23+, since
      # pipe_traversal doesn't have a right seclinux policy. In this case,
      # Just use real adb.
      self._use_real_adb = (self._IsUserBuild(build_prop)
                            and self.GetApiVersion() >= 23)
    else:
      assert self.GetApiVersion() < 19, 'Yaffs format used in new image'

    if userdata_tarball:
      # userdata tarball should contain:
      #   self._UserdataQemuFile()
      #   self._RamdiskFile()
      #   self._CacheFile()
      #   self._SdcardFile()
      #   self._SnapshotFile()
      #
      # It does not include:
      #   self._KernelFile()  # handled above
      #   self._SystemFile()  # handled above
      #   self._InitSystemFile() # handled above
      subprocess.check_call(['tar', '-xzSf', userdata_tarball, '-C',
                             self._images_dir])
      data_size = FLAGS.data_partition_size
      if (self.GetApiVersion() >= 19 and data_size and
          data_size > os.path.getsize(self._UserdataQemuFile()) >> 20):
        logging.info('Resize data partition to %dM', data_size)
        subprocess.check_call(['/sbin/resize2fs', '-f',
                               self._UserdataQemuFile(), '%dM' % data_size])
    else:
      # need to setup:
      #   self._RamdiskFile() - we modify this abit
      #   self._SnapshotFile() - always exists
      self._InitializeRamdisk(system_image_dir)
      self._SparseCp(self.android_platform.empty_snapshot_fs,
                     self._SnapshotFile())

    if not os.path.exists(self._UserdataQemuFile()):
      init_data = os.path.join(system_image_dir, 'userdata.img')
      if os.path.exists(init_data):
        self._SparseCp(init_data, self._UserdataQemuFile())
      else:
        init_data = '%s.tar.gz' % init_data
        assert os.path.exists(init_data), '%s: userdata.img?' % system_image_dir
        self._ExtractTarEntry(
            init_data,
            'userdata.img',
            os.path.dirname(self._UserdataQemuFile()))
        shutil.move(os.path.join(os.path.dirname(self._UserdataQemuFile()),
                                 'userdata.img'),
                    self._UserdataQemuFile())

    if not os.path.exists(self._CacheFile()):
      if use_ext4:
        init_cache = resources.GetResourceFilename(
            'android_test_support/'
            'tools/android/emulator/support/cache.img.tar.gz')
        self._ExtractTarEntry(
            init_cache, 'cache.img', os.path.dirname(self._CacheFile()))
      else:
        assert self.GetApiVersion() < 19, 'Yaffs format used in new image'
        with open(self._CacheFile(), 'wb') as unused_cache_file:
          pass  # just need to create the file here.

    if not os.path.exists(self._SdcardFile()):
      try:
        self._SparseCp(
            resources.GetResourceFilename(
                'android_test_support/'
                'tools/android/emulator/support'
                '/default_sdcard.%s.img' % self._metadata_pb.sdcard_size_mb),
            self._SdcardFile())
        logging.info('using default sd card.')
      except IOError:
        logging.info('trying to make sdcard on the fly.')
        sdcard_args = [
            self.android_platform.mksdcard,
            '-l',
            'testSdCard',
            '%sM' % self._metadata_pb.sdcard_size_mb,
            self._SdcardFile()]
        timer.start(_SDCARD_CREATE)
        common.SpawnAndWaitWithRetry(sdcard_args)
        timer.stop(_SDCARD_CREATE)

    os.chmod(self._SdcardFile(), stat.S_IRWXU)
    os.chmod(self._UserdataQemuFile(), stat.S_IRWXU)
    os.chmod(self._CacheFile(), stat.S_IRWXU)
    os.chmod(self._SnapshotFile(), stat.S_IRWXU)
    os.chmod(self._SystemFile(), stat.S_IRWXU)

  # pylint: disable=too-many-statements
  def _MakeAvd(self):
    """Crafts a set of ini files to correspond to an avd for this device.

    AVD is the only way to pass certain properties on to the emulated device,
    most notably dpi-device and vm heapsize (both of which are ignored from the
    command line). Unfortunately there are options which are only controllable
    from commandline (instead of avd) so we get to configure things thru both
    interfaces. One day I hope the configuration style will all be unified into
    one rational method which is effective both thru ADT/eclipse and
    programatically. (As you're about to see, programmatically creating AVDs is
    a bit of a trip!).

    Returns:
      an appropriate avd_name to pass to the emulator.
    """
    # When using AVDs, the emulator expects to find AVDs beneath
    #
    # $ANDROID_SDK_HOME/.android/avd/[avd_name].
    # if unset, this defaults to $HOME or /tmp
    # both of these are undesired in our case.
    #
    # Also when using AVDs the emulator wants to find $ANDROID_SDK_ROOT
    # and expects skin info to be stored beneath that location. We will
    # in the future need to support skins.
    avd_files = self._TempDir('avd_files')
    android_tmp_dir = os.path.join(avd_files, 'tmp')
    home_dir = os.path.join(avd_files, 'home')
    os.makedirs(android_tmp_dir)
    os.makedirs(home_dir)
    # New version of emulator check for these directories.
    os.makedirs(os.path.join(self._images_dir, 'platforms'))
    os.makedirs(os.path.join(self._images_dir, 'platform-tools'))

    self._emulator_env['ANDROID_SDK_ROOT'] = self._images_dir
    self._emulator_env['ANDROID_SDK_HOME'] = home_dir
    self._emulator_env['HOME'] = home_dir
    self._emulator_env['ANDROID_TMP'] = android_tmp_dir

    self._console_auth_token_file = os.path.join(home_dir,
                                                 '.emulator_console_auth_token')
    if not self._enable_console_auth:
      # Write an empty file to disable console auth.
      with open(self._console_auth_token_file, 'w+') as f:
        f.write('')

    dot_android_dir = os.path.join(home_dir, '.android')
    os.makedirs(dot_android_dir)
    ddms_cfg_file = os.path.join(dot_android_dir, 'ddms.cfg')
    with open(ddms_cfg_file, 'w+') as ddms_cfg:
      # suppress the 'welcome to android' dialog
      ddms_cfg.write('pingOptIn=false\n')
      ddms_cfg.write('pingTime.emulator=1348614108574\n')
      ddms_cfg.write('pingId=592273184351987827\n')

    dot_config_dir = os.path.join(home_dir, '.config',
                                  'Android Open Source Project')
    os.makedirs(dot_config_dir)
    emulator_cfg_file = os.path.join(dot_config_dir, 'Emulator.conf')
    with open(emulator_cfg_file, 'w+') as emulator_cfg:
      # suppress some dialogs
      emulator_cfg.write('[General]\n')
      emulator_cfg.write('showAdbWarning=false\n')
      emulator_cfg.write('showAvdArchWarning=false\n')

    avd_dir = os.path.join(home_dir, '.android', 'avd')
    # Allowed chars are:
    # ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-
    avd_name = 'mobile_ninjas.adb.%s' % self.emulator_adb_port
    content_dir = os.path.join(avd_dir, avd_name)
    os.makedirs(content_dir)

    root_config_file = os.path.join(avd_dir, '%s.ini' % avd_name)
    with open(root_config_file, 'w+') as root_config:
      root_config.write('path=%s\n' % self._SessionImagesDir())
      root_config.write('target=android-%s\n' % self._metadata_pb.api_name)

    user_cfg_file = os.path.join(self._SessionImagesDir(), 'emulator-user.ini')
    with open(user_cfg_file, 'w+') as user_cfg:
      # Always put emulator window in fixed position.
      user_cfg.write('window.x = 0\n')
      user_cfg.write('window.y = 0\n')

    config_ini_file = os.path.join(self._SessionImagesDir(), 'config.ini')
    with open(config_ini_file, 'w+') as config_ini:
      for prop in self._metadata_pb.avd_config_property:
        config_ini.write('\n%s=%s\n' % (prop.name, prop.value))

      # the default size is ~256 megs, which fills up fast on iterative
      # development.
      if 'ext4' in subprocess.check_output(['file', self._UserdataQemuFile()]):
        # getting this size right is pretty crucial - if it doesnt match
        # the underlying file the guest os will get confused.
        config_ini.write('disk.dataPartition.size=%s\n' %
                         os.path.getsize(self._UserdataQemuFile()))
      else:
        config_ini.write('disk.dataPartition.size=2047m\n')

      # system partition must be less than 2GB (there's a constraint check in
      # qemu). Also we must set the commandline flag too - which sets both
      # userdata and system sizes, so everything is set to 2047 for sanity.
      if 'ext4' in subprocess.check_output(['file', self._SystemFile()]):
        # getting this size right is pretty crucial - if it doesnt match
        # the underlying file the guest os will get confused.
        config_ini.write('disk.systemPartition.size=%s\n' %
                         os.path.getsize(self._SystemFile()))
      else:
        config_ini.write('disk.systemPartition.size=2047m\n')

      # a link back to our original name, not sure if this is needed, but lets
      # be consistant.
      config_ini.write('avd.name=%s\n' % avd_name)

      config_ini.write('image.sysdir.1=%s\n' % 'session')
      config_ini.write('image.sysdir.2=%s\n' % 'init')

      # if we do not set this - android just creates cache by itself.
      config_ini.write('disk.cachePartition=1\n')
      config_ini.write('disk.cachePartition.path=cache.img\n')
      cache_size = '66m'
      if 'ext4' in subprocess.check_output(['file', self._CacheFile()]):
        cache_size = os.path.getsize(self._CacheFile())

      # getting this size right is pretty crucial - if it doesnt match
      # the underlying file the guest os will get confused.
      config_ini.write('disk.cachePartition.size=%s\n' % cache_size)

      if self._metadata_pb.with_adbd_pipe:
        config_ini.write('adbd.over.pipe=1\n')

      # this really could be determined by the emulator-arm vs emulator-x86
      # binary itself, and when we run without any AVD at all, it does. However
      # once we start specifying avds, we need to re-specify this info in the
      # avd, otherwise the emulator will balk.

      avd_cpu_arch = self._metadata_pb.emulator_architecture
      if avd_cpu_arch.startswith('arm'):
        avd_cpu_arch = 'arm'
      # else hopefully source properties matches. sigh!

      config_ini.write('hw.cpu.arch=%s\n' % avd_cpu_arch)
      config_ini.write('hw.cpu.ncore=%d\n' % FLAGS.cores)

      if not FLAGS.hardware_keyboard:
        config_ini.write('hw.keyboard=no\n')

      # and there are race conditions if skin and the hw ini values do not agree
      # with each other.
      skin = self._metadata_pb.skin
      height = skin[skin.index('x') + 1:]
      width = skin[:skin.index('x')]
      config_ini.write('hw.lcd.width=%s\n' % width)
      config_ini.write('hw.lcd.height=%s\n' % height)
      # there are other avd pieces we omit, because they're overridden by the
      # flags we pass in from the commandline.

    return avd_name
  # pylint: enable=too-many-statements

  def _GetProperty(self, property_name, default_properties,
                   source_properties, default_value):
    default_properties = default_properties or {}
    source_properties = source_properties or {}
    key = 'avd_config_ini.%s' % property_name
    key = key.lower()

    return default_properties.get(key, source_properties.get(key,
                                                             default_value))

  def _MapToSupportedDensity(self, density):
    """Map density to emulator supported buckets."""

    if density == _DENSITY_TVDPI:
      return density
    # The reference source code is here:
    # https://android.googlesource.com/platform/external/qemu/+/emu-2.2-release/android/hw-lcd.c#18
    # It finds the closest (either higher or lower) supported dpi for a given
    # density.
    for i in range(len(_BUCKET_DPI[:-1])):
      if density < ((_BUCKET_DPI[i] + _BUCKET_DPI[i+1])/2):
        return _BUCKET_DPI[i]
    return _BUCKET_DPI[-1]

  def Configure(self, system_image_dir, skin, memory,
                density, vm_heap, net_type='fastnet',
                source_properties=None, default_properties=None,
                kvm_present=False, system_image_path=None):
    """Performs pre-start configuration of the emulator."""
    assert os.path.exists(system_image_dir), ('Sysdir doesnt exist: %s' %
                                              system_image_dir)
    if not system_image_path:
      if os.path.exists(os.path.join(system_image_dir, 'system.img')):
        system_image_path = os.path.join(system_image_dir, 'system.img')
      else:
        system_image_path = os.path.join(system_image_dir, 'system.img.tar.gz')

    self._metadata_pb = emulator_meta_data_pb2.EmulatorMetaDataPb(
        system_image_dir=system_image_dir,
        skin=skin,
        memory_mb=int(memory),
        density=int(density),
        net_type=net_type,
        vm_heap=int(vm_heap),
        net_delay=NET_TYPE_TO_DELAY[net_type],
        net_speed=NET_TYPE_TO_SPEED[net_type],
        sdcard_size_mb=int(256),
        api_name=source_properties[API_LEVEL_KEY],
        emulator_architecture=self._DetermineArchitecture(source_properties),
        with_kvm=self._WithKvm(source_properties, kvm_present),
        with_adbd_pipe=False,
        with_patched_adbd=False,
        supports_gpu=self._SupportsGPU(source_properties),
        supported_open_gl_drivers=self._DetermineSupportedDrivers(
            source_properties),
        sensitive_system_image=self._DetermineSensitiveImage(source_properties),
        system_image_path=system_image_path
    )

    if self._metadata_pb.with_kvm:
      self._connect_poll_interval /= 4.0
      self._connect_max_attempts *= 4

    if default_properties:  # allow any user specified readonly props to take
      # precedence over our set of ro.test_harness
      # Ignores avd_config_ini. properties. They are used
      # to store device specific config.ini values.
      for prop_name, prop_value in default_properties.items():
        if not prop_name.startswith('avd_config_ini.'):
          self._metadata_pb.boot_property.add(name=prop_name, value=prop_value)

      # need to allow users to specify device specific sd card sizes in
      # default.properties.
      self._metadata_pb.sdcard_size_mb = int(default_properties.get(
          SDCARD_SIZE_KEY, 256))

      self._metadata_pb.emulator_type = (
          emulator_meta_data_pb2.EmulatorMetaDataPb.EmulatorType.Value(
              default_properties.get(EMULATOR_TYPE_KEY, 'QEMU').upper()))

    self._metadata_pb.qemu_arg.extend(
        self._DetermineQemuArgs(source_properties, kvm_present))
    self._metadata_pb.boot_property.add(
        name='debug.sf.nobootanimation',  # disable boot animation by default
        value='1')

    self._metadata_pb.boot_property.add(
        name='ro.test_harness',  # allows for bypassing permission screens
        value='1')

    self._metadata_pb.boot_property.add(
        name='ro.monkey',  # allows for bypassing permission screens pre ICS
        value='1')

    self._metadata_pb.boot_property.add(
        name='ro.setupwizard.mode',  # skip past the intro screens.
        value='DISABLED')

    self._metadata_pb.boot_property.add(
        name='ro.lockscreen.disable.default',  # disable lockscreen (jb & up)
        value='1')

    # emulator supports bucketed densities. Map the provided density into
    # the correct bucket.
    self._metadata_pb.density = self._MapToSupportedDensity(
        self._metadata_pb.density)

    # QEMU is supposed to set qemu.sf.lcd_density - however in setting this
    # variable it races with SurfaceFlinger to read it. If SF checks it first
    # before QEMU sets it, we'll get whonky density values. We set
    # ro.sf.lcd_density to the same value QEMU will set qemu.sf.lcd_density -
    # this eliminates the race.
    self._metadata_pb.boot_property.add(
        name='ro.sf.lcd_density',
        value=str(self._metadata_pb.density))
    self._metadata_pb.boot_property.add(
        name='qemu.sf.lcd_density',
        value=str(self._metadata_pb.density))
    # Also eliminates the race that we lost camera sometimes.
    # 'back' is the current default value for emulator.
    self._metadata_pb.boot_property.add(
        name='qemu.sf.fake_camera', value='back')

    self._metadata_pb.boot_property.add(
        name='service.adb.root', value='1')

    if self.GetApiVersion() == 19:
      # Work around for one opengl bug (b/32765858) in kitkat.
      self._metadata_pb.boot_property.add(
          name='debug.hwui.render_dirty_regions', value='false')

    # If the user has not specified heapgrowth limit in default properties,
    # default it to either 64 of vm_heap, whichever is lower.
    if not [kv for kv in self._metadata_pb.boot_property
            if kv.name == HEAP_GROWTH_LIMIT_KEY]:
      vm_heap = self._metadata_pb.vm_heap
      self._metadata_pb.boot_property.add(
          name=HEAP_GROWTH_LIMIT_KEY,
          value='%sm' % min(64, vm_heap)
      )

    # We set this value in AVD's also, however in certain cases (for example:
    # gingerbread) it is not set early enough to have an impact. By writing
    # it into the boot_property file we ensure it'll be there as soon as the
    # system starts.

    self._metadata_pb.boot_property.add(
        name='dalvik.vm.heapsize',
        value='%sm' % self._metadata_pb.vm_heap)

    # disable dex pre-verification. Verification is still done, but at runtime
    # instead of installation time.
    #
    # We do this to allow for the case where the production and test apks both
    # contain the same class. With preverification turned on, this situation
    # will result in a dalvik failure (because verification was done at
    # installation time and the verified expected the app apk to be completely
    # self contained). Since bazel will ensure that app and test apk are using
    # the same dependencies this check is superflous in our case.
    if self.GetApiVersion() <= 20:
      # no longer applicable in ART world.
      self._metadata_pb.boot_property.add(
          name='dalvik.vm.dexopt-flags',
          value='v=n,o=v')

    # Yes double specify the timezone. The emulator commandline setting works
    # for older versions of Android - and newer versions of android respect
    # this property setting.
    self._metadata_pb.boot_property.add(
        name='persist.sys.timezone',
        value='America/Los_Angeles')

    prop = self._metadata_pb.avd_config_property.add(name='hw.mainKeys')
    prop.value = self._GetProperty(prop.name, default_properties,
                                   source_properties, 'yes')

    # Keyboard support for real keyboard.
    # emulator bug - documentation says default value is "yes".

    prop = self._metadata_pb.avd_config_property.add(name='hw.keyboard')
    prop.value = self._GetProperty(prop.name, default_properties,
                                   source_properties, 'yes')

    if self.GetApiVersion() != 15:
      # Allow user to switch back to softkeyboard.
      # in ICS this is broken - emulator will appear in landscape mode if this
      # is set.
      prop = self._metadata_pb.avd_config_property.add(name='hw.keyboard.lid')
      prop.value = self._GetProperty(prop.name, default_properties,
                                     source_properties, 'yes')

    # This forces a virtual sound card to be presented to android.
    # whether or not we do anything with this sound card is controlled by
    # the -audio commandline flag.
    self._metadata_pb.avd_config_property.add(
        name='hw.audioOutput',
        value='yes')
    self._metadata_pb.avd_config_property.add(
        name='hw.audioInput',
        value='yes')

    # emulator bug - dpi-device is ignored from the commandline
    self._metadata_pb.avd_config_property.add(
        name='hw.lcd.density',
        value=str(self._metadata_pb.density))

    # people always think the backlight simulation is some sort of indication
    # that the device is going to sleep or some low power mode and thats why
    # their tests are flaky, it's not the reason, disable it.
    self._metadata_pb.avd_config_property.add(
        name='hw.lcd.backlight',
        value='no')

    # since this ini file is parsed after our --boot_property flags are parsed
    # we must set this here (otherwise it applies our boot_prop flag and then
    # the default value of this flag (overwriting us!)
    self._metadata_pb.avd_config_property.add(
        name='vm.heapSize',
        value=str(self._metadata_pb.vm_heap))

  def _SanityCheckOpenGLDriver(self, open_gl_driver,
                               allow_experimental_open_gl):
    assert open_gl_driver in OPEN_GL_DRIVERS, (
        '%s: unknown driver.' % open_gl_driver)
    driver_good = open_gl_driver in self._metadata_pb.supported_open_gl_drivers
    assert allow_experimental_open_gl or driver_good, (
        '%s: not in supported.' % open_gl_driver)
    if allow_experimental_open_gl and not driver_good:
      logging.info('%s: is not supported - but trying anyway.', open_gl_driver)

  def StartDevice(self, enable_display, start_vnc_on_port=0, net_type='fastnet',
                  userdata_tarball=None, new_process_group=False,
                  window_scale=None, with_audio=False,
                  with_boot_anim=False, emulator_tmp_dir=None,
                  open_gl_driver=None,
                  allow_experimental_open_gl=False,
                  build_time_only_no_op_rendering=False):
    """Launches an emulator process."""
    assert self._metadata_pb, 'Not configured!'
    self._emulator_tmp_dir = emulator_tmp_dir or tempfile.mkdtemp()
    if build_time_only_no_op_rendering:
      self._display = None
    else:
      open_gl_driver = open_gl_driver or NO_OPEN_GL
      self._SanityCheckOpenGLDriver(open_gl_driver, allow_experimental_open_gl)
      self._display = Display(
          skin=self._metadata_pb.skin,
          tmp_dir=self._emulator_tmp_dir,
          enable_display=enable_display,
          start_vnc_on_port=start_vnc_on_port,
          open_gl_driver=open_gl_driver,
          env=os.environ)

    start_timer = stopwatch.StopWatch()
    start_timer.start()
    start_timer.start(_STAGE_DATA)
    self._StageDataFiles(self._metadata_pb.system_image_dir,
                         self._metadata_pb.system_image_path, userdata_tarball,
                         start_timer, open_gl_driver == GUEST_OPEN_GL)
    start_timer.stop(_STAGE_DATA)

    start_timer.start(_START_PROCESS)
    self._StartEmulator(start_timer, net_type, new_process_group, window_scale,
                        with_audio, with_boot_anim)
    start_timer.stop(_START_PROCESS)
    start_timer.stop()
    self._AddTimerResults('start_device', start_timer)

  def _RuntimeProperties(self):
    """Return properties which could be tune at run time with flags."""
    ret = []
    if FLAGS.boost_dex2oat:
      ret.append(Properties(name='dalvik.vm.dex2oat-filter',
                            value='interpret-only'))
    return ret

  # pylint: disable=too-many-statements
  def _InitializeRamdisk(self, system_image_dir):
    """Pushes the boot properties to RAM Disk."""
    base_ramdisk = os.path.join(system_image_dir, 'ramdisk.img')
    ramdisk_dir = self._TempDir('ramdisk_repack')
    exploded_temp = os.path.join(ramdisk_dir, 'tmp')
    os.makedirs(exploded_temp)

    gunzip_proc = subprocess.Popen(
        ['gunzip', '-f', '-c', base_ramdisk],
        stdout=subprocess.PIPE)
    extract_cpio_proc = subprocess.Popen(
        ['cpio', '--extract'],
        cwd=exploded_temp,
        stdin=gunzip_proc.stdout,
        stdout=open('/dev/null'))
    gunzip_proc.stdout.close()
    extract_cpio_proc.wait()
    gunzip_proc.wait()

    assert os.path.exists(
        os.path.join(exploded_temp, 'default.prop')
    ), 'default.prop does not exist in ramdisk.'

    properties = '#\n# MOBILE_NINJAS_PROPERTIES\n#\n'
    for prop in self._metadata_pb.boot_property:
      properties += '%s=%s\n' % (prop.name, prop.value)
    properties += '#\n# MOBILE_NINJAS_RUNTIME_PROPERTIES\n#\n'
    for prop in self._RuntimeProperties():
      properties += '%s=%s\n' % (prop.name, prop.value)
    properties += '#\n# MOBILE_NINJAS_PROPERTIES_END\n#\n\n'
    with open(os.path.join(exploded_temp, 'default.prop'), 'r+') as prop_file:
      properties += prop_file.read()
      prop_file.seek(0)
      prop_file.write(properties)

    with open(os.path.join(exploded_temp, 'init.rc'), 'r+') as init_rc:
      in_adbd = False
      # note: do not use for line in init_rc. it reads large buffers
      # of init.rc into memory (updating file position). this makes
      # it hard for us to write back to the file into the correct
      # position once we encounter adbd's disabled line.
      line = init_rc.readline()
      while line:
        if not in_adbd:
          if line.startswith('service adbd'):
            in_adbd = True
        else:
          if self._metadata_pb.with_patched_adbd and ('disable' in line
                                                      or 'seclabel' in line):
            # I would _LOVE_ to have the seclabels checked on adbd.
            #
            # However I would love to reliably connect to adbd from multiple
            # adb servers even more.
            #
            # Post KitKat adbd stopped allowing multiple adb servers to talk
            # to it. So on post KitKat devices, we have to push an old (read
            # good, working, useful) version of adbd onto the emulator. This
            # version of adbd may not be compatible with the selinux policy
            # enforced on adbd. Therefore we disable that singular policy.
            #
            # TL;DR;. Given the fact that we have 4 choices:
            #
            # #1 use a broken adbd
            # #2 replace adbd with a working one and disable SELinux entirely
            # #3 replace adbd with a working one and disable the adbd seclabel
            # #4 fix adbd
            #
            # 4 is the most desirable - but outside our scope - 3 seems the
            # least harmful and most effective.
            #
            # I just want to freaking copy some bytes and exec a few shell
            # commands, is that so wrong? :)

            init_rc.seek(- len(line), 1)
            # comment it out!
            init_rc.write('#')
            init_rc.readline()
          else:
            if line.startswith('service ') or line.startswith('on '):
              in_adbd = False
        line = init_rc.readline()

      # at end of file.
      init_rc.write('\n')

      init_rc.write(
          'service g3_monitor /system/bin/app_process /system/bin com.google.'
          'android.apps.common.testing.services.activitycontroller.'
          'ActivityControllerMain\n')
      init_rc.write('    setenv CLASSPATH /g3_activity_controller.jar\n')
      init_rc.write('    disabled\n')  # property triggers will start us.
      init_rc.write('    user system\n')
      init_rc.write('    group system\n')

      # trigger as soon as service manager is ready.
      init_rc.write('\n')
      init_rc.write('on property:init.svc.servicemanager=running\n')
      init_rc.write('    start g3_monitor\n')

      # if zygote dies or restarts, we should restart so we can connect to the
      # new system server.
      init_rc.write('\n')
      init_rc.write('on service-exited-zygote\n')
      init_rc.write('    stop g3_monitor\n')
      init_rc.write('    start g3_monitor\n')
      init_rc.write('\n')

      # In this stanza we're setting up pipe_traversal for shell / push
      # and pull commands, it connects thru qemu-pipes to a suite of
      # sockets beneath $EMULATOR_CWD/sockets
      init_rc.write('service pipe_traverse /sbin/pipe_traversal ')
      init_rc.write('--action=emu-service\n')
      init_rc.write('    user root\n')
      init_rc.write('    group root\n')
      if self.GetApiVersion() >= 23:
        init_rc.write('    seclabel u:r:shell:s0\n')
      init_rc.write('\n')

      # Set up pipe_traversal to allow guest to connect to its own
      # Android telnet console. Also, apparently service names have a
      # maximum length of 16 characters.
      init_rc.write('service tn_pipe_traverse /sbin/pipe_traversal ')
      init_rc.write('--action=raw ')
      init_rc.write(
          '--external_addr=tcp-listen::%d ' % _DEFAULT_QEMU_TELNET_PORT)
      init_rc.write('--relay_addr=qemu-pipe:pipe:unix:sockets/qemu.mgmt ')
      init_rc.write('--frame_relay\n')
      init_rc.write('    user root\n')
      init_rc.write('    group root\n')
      if self.GetApiVersion() >= 23:
        init_rc.write('    seclabel u:r:shell:s0\n')
      init_rc.write('\n')

      init_rc.write('on boot\n')
      init_rc.write('   start pipe_traverse\n')
      init_rc.write('   start tn_pipe_traverse\n')
      init_rc.write('\n')

    arch = self._metadata_pb.emulator_architecture
    pipe_traversal_path = os.path.join(exploded_temp, 'sbin', 'pipe_traversal')
    shutil.copy2(
        resources.GetResourceFilename(
            'android_test_support/'
            'tools/android/emulator/daemon/%s/pipe_traversal' % arch),
        pipe_traversal_path)
    os.chmod(pipe_traversal_path, stat.S_IRWXU)

    # FYI: /sbin is only readable by root, so we put g3_activity_controller.jar
    # in / since it is run by the system user.
    shutil.copy2(
        resources.GetResourceFilename(
            'android_test_support/'
            'tools/android/emulator/daemon/g3_activity_controller.jar'),
        os.path.join(exploded_temp, 'g3_activity_controller.jar'))

    os.chmod(os.path.join(exploded_temp, 'g3_activity_controller.jar'),
             stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)

    if self._metadata_pb.with_patched_adbd:
      # hrm I wonder how borked ADBD is on this device.
      # oh well!!!
      resource_adb_path = os.path.join(
          'android_test_support', 'tools', 'android', 'emulator', 'daemon',
          self._metadata_pb.emulator_architecture, 'adbd')
      adbd_ramdisk_path = os.path.join(exploded_temp, 'sbin', 'adbd')
      with open(adbd_ramdisk_path, 'w+') as ramdisk_adbd:
        with contextlib.closing(
            resources.GetResourceAsFile(resource_adb_path)) as resource_adbd:
          adbd_bytes = resource_adbd.read()
          ramdisk_adbd.write(adbd_bytes)
          ramdisk_adbd.flush()

    find_proc = subprocess.Popen(
        ['find', '.', '-mindepth', '1', '-printf', '%P\n'],
        cwd=exploded_temp,
        stdout=subprocess.PIPE)
    create_cpio_proc = subprocess.Popen(
        ['cpio', '--create', '--format', 'newc', '--owner', '0:0'],
        cwd=exploded_temp,
        stdin=find_proc.stdout,
        stdout=subprocess.PIPE)
    gzip_proc = subprocess.Popen(
        ['gzip', '-c'],
        stdin=create_cpio_proc.stdout,
        stdout=open(self._RamdiskFile(), 'w+'))
    find_proc.stdout.close()
    create_cpio_proc.stdout.close()
    gzip_proc.wait()
    create_cpio_proc.wait()
    find_proc.wait()
  # pylint: enable=too-many-statements

  def _NeedBootGL(self):
    """Check if we need OpenGL at boot stage."""
    return self.GetApiVersion() > 23 and self._display is None

  def _MakeEmulatorEnv(self, parent_env):
    """Sets up (most) of the environment vars for the emulator.

    General rule of thumbs-
      #1 Do not overwrite the vars setup here later on.
      #2 If the proper var value can be determined without modifying the
         filesystem, do it here.

    Args:
      parent_env: typically os.environ

    Returns:
      The basis of the emulator's environment vars.
    """
    lib_paths = [self.android_platform.prepended_library_path]
    gl_base = os.path.join(self.android_platform.base_emulator_path, 'lib64')
    gles_mesa = os.path.join(gl_base, 'gles_mesa')
    qt_lib = os.path.join(gl_base, 'qt/lib')
    lib_paths.append(qt_lib)
    # Make sure we always have GL library in search path.
    # Either is mesa GL or host GL.
    if not self._display or self._display.open_gl_driver != HOST_OPEN_GL:
      lib_paths.append(gles_mesa)
    else:
      out = subprocess.check_output(['ldd', self.android_platform.emulator_x86])
      for line in out.splitlines():
        # line looks like:
        #   libGL.so.1 => /usr/lib/nvidia-367/libGL.so.1 (0x00007fbcd1c4b000)
        match = re.search(r'libGL.so.1 => (.*libGL.so.1)', line)
        if match:
          lib_paths.append(os.path.dirname(match.group(1)))

    # Use GL translator libraries only if opengl is enabled.
    if (self._NeedBootGL() or self._display and
        self._display.open_gl_driver != NO_OPEN_GL and
        self._display.open_gl_driver != GUEST_OPEN_GL):
      lib_paths.append(gl_base)

    lib_paths.extend([self.android_platform.emulator_support_lib_path,
                      parent_env.get('LD_LIBRARY_PATH')])

    lib_paths = [l for l in lib_paths if l]

    emu_path = self.android_platform.base_emulator_path
    target_env = {
        'LD_LIBRARY_PATH': ':'.join(lib_paths),
        'LD_DEBUG': parent_env.get('LD_DEBUG'),
        'KVM_DEVICE': self.android_platform.kvm_device,
        'ANDROID_EMULATOR_KVM_DEVICE': self.android_platform.kvm_device,
        'SDL_VIDEO_X11_WMCLASS': 'Google Android Emulator',
        'QT_XKB_CONFIG_ROOT': self.android_platform.xkb_path,
        'ANDROID_EMULATOR_LAUNCHER_DIR': emu_path,
        'ANDROID_QT_QPA_PLATFORM_PLUGIN_PATH': os.path.join(
            emu_path, 'lib64/qt/plugins'),
    }

    # disable emulator-XXXX from adb devices on .
    if not FLAGS.skip_connect_device:
      target_env['ANDROID_ADB_SERVER_PORT'] = '1'

    if self._NeedBootGL() or self._display and self._display.open_gl_driver in (
        MESA_OPEN_GL, SWIFTSHADER_OPEN_GL):
      target_env['LIBGL_DEBUG'] = 'verbose'
      target_env['EGL_LOG_LEVEL'] = 'debug'
      if (self._NeedBootGL() or
          self._display.open_gl_driver == SWIFTSHADER_OPEN_GL):
        sdir = os.path.join(self.android_platform.base_emulator_path,
                            'lib64/gles_swiftshader')
        target_env['ANDROID_EGL_LIB'] = os.path.join(sdir, 'libEGL.so')
        target_env['ANDROID_GLESv1_LIB'] = os.path.join(sdir, 'libGLES_CM.so')
        target_env['ANDROID_GLESv2_LIB'] = os.path.join(sdir, 'libGLESv2.so')
      else:
        # MESA
        target_env['MESA_DEBUG'] = 'verbose'
        # emulator binary use these special variables to decide whether to go
        # some different code path to bypass some bugs. It's set in emulator
        # launcher. But since we don't use emulator launcher from emulator team
        # now, we have to set it ourselves.
        # TODO: change to use emulator launcher and remove this hack.
        target_env['ANDROID_GL_LIB'] = 'mesa'
        target_env['ANDROID_GL_SOFTWARE_RENDERER'] = '1'

    return {k: str(v) for k, v in target_env.items() if v is not None}

  def _AddTimerResults(self, activity_name, timer):
    pb_timings = []
    for name, acc_time, starts in timer.results(verbose=True):
      pb_timings.append(emulator_meta_data_pb2.TimerPb(
          name=name,
          accumulated_time=long(acc_time * 1000),
          number_of_starts=starts))

    self._metadata_pb.perf_data.add(
        activity_name=activity_name,
        timing=pb_timings)


  # pylint: disable=too-many-statements
  def _PrepareQemuArgs(self, binary, net_type, window_scale, with_audio,
                       with_boot_anim):
    """Prepare args for calling emulator."""
    self._emulator_start_args = [
        binary,
        '-ports', '%s,%s' % (self.emulator_telnet_port,
                             self.emulator_adb_port),
        '-skin', self._metadata_pb.skin,
        '-cache', 'cache.img',  # only respected via cmdline flag.
        '-data', 'userdata-qemu.img',  # only respected via cmdline flag.
        '-memory', str(self._metadata_pb.memory_mb),
        '-sdcard', 'sdcard.img',
        '-partition-size', '2047',
        '-no-snapshot-save',
        '-verbose',
        '-unix-pipe', 'sockets/qemu.mgmt',
        '-unix-pipe', 'sockets/device-forward-server',
        '-unix-pipe', 'sockets/tar-pull-server',
        '-unix-pipe', 'sockets/exec-server',
        '-unix-pipe', 'sockets/tar-push-server',
        '-writable-system',
        '-show-kernel']

    if (self._metadata_pb.emulator_type ==
        emulator_meta_data_pb2.EmulatorMetaDataPb.QEMU):
      # TODO(b/31431334): fix -timezone in qemu2
      self._emulator_start_args.extend(['-timezone', 'America/Los_Angeles'])
    else:
      self._emulator_start_args.extend(['-engine', 'qemu2',
                                        '-kernel', self._KernelFile()])

    if not self._display:
      self._emulator_start_args.append('-no-window')
      if self._NeedBootGL():
        self._emulator_start_args.extend(['-gpu', 'on'])

    if (not self._enable_gps and
        self._metadata_pb.emulator_type ==
        emulator_meta_data_pb2.EmulatorMetaDataPb.QEMU):
      self._emulator_start_args.extend(['-gps', 'null'])

    if not with_audio:
      if (self._metadata_pb.emulator_type ==
          emulator_meta_data_pb2.EmulatorMetaDataPb.QEMU2):
        self._emulator_start_args.extend(['-no-audio'])
      else:
        self._emulator_start_args.extend(['-audio', 'none'])

    if not with_boot_anim:
      self._emulator_start_args.append('-no-boot-anim')

    se_linux_mode = [prop for prop in self._metadata_pb.boot_property
                     if prop.name == 'ro.initial_se_linux_mode']

    if se_linux_mode:
      assert len(se_linux_mode) == 1, 'Too many values: %s' % se_linux_mode
      se_linux_mode = se_linux_mode[0].value
      valid_modes = ['disabled', 'permissive']
      assert se_linux_mode in valid_modes, ('%s invalid. Only options are: %s'
                                            '. If not specified and API > 19 '
                                            'defaults to enforcing.' % (
                                                se_linux_mode, valid_modes))
      self._emulator_start_args.extend(['-selinux', se_linux_mode])

    if window_scale:
      self._emulator_start_args.extend(
          ['-scale', str(window_scale / 100.0)])
    if not window_scale or window_scale == 100:
      self._emulator_start_args.append('-fixed-scale')
    if (self._display and self._display.open_gl_driver != NO_OPEN_GL and
        self._display.open_gl_driver != GUEST_OPEN_GL):
      self._emulator_start_args.extend(['-no-snapshot-load', '-gpu', 'on'])

    if net_type is None or net_type == 'off':
      net_delay = self._metadata_pb.net_delay
      net_speed = self._metadata_pb.net_speed
    else:
      net_delay = NET_TYPE_TO_DELAY[net_type]
      net_speed = NET_TYPE_TO_SPEED[net_type]
    self._emulator_start_args.extend(
        ['-netdelay', net_delay, '-netspeed', net_speed])

    avd_name = self._MakeAvd()
    self._emulator_start_args.extend(['-avd', avd_name])

    if (self._metadata_pb.qemu_arg or
        self._qemu_gdb_port or
        self._enable_single_step or
        net_type == 'off' or
        not self._enable_g3_monitor):
      self._emulator_start_args.append('-qemu')

      if self._metadata_pb.qemu_arg:
        self._emulator_start_args.extend(self._metadata_pb.qemu_arg)
        self._emulator_start_args.extend(
            ['-L', self.android_platform.MakeBiosDir(self._TempDir('bios'))])

      if self._qemu_gdb_port:
        self._emulator_start_args.extend(['-gdb',
                                          'tcp::%d' % self._qemu_gdb_port])
      if self._enable_single_step:
        self._emulator_start_args.append('-S')

      if net_type == 'off':
        # TODO: fix this for IPV6
        # We always want to allow tcp connections to host for testing purpose.
        # BTW, there is a bug in emulator, so we have to use 1-65534 instead of
        # 1-65535.
        self._emulator_start_args.extend(['-drop-tcp', '-drop-udp',
                                          '-allow-tcp', '10.0.2.2:[1-65534]'])

      if not self._enable_g3_monitor:
        # init process of Android will set a system property begin with
        # 'ro.kernel' for every key=value pair added here.
        # See:
        # https://android.googlesource.com/platform/system/core/+/gingerbread/init/init.c#424
        self._emulator_start_args.extend(['-append', 'g3_monitor=0'])

  # pylint: disable=too-many-statements
  def _StartEmulator(self, timer,
                     net_type, new_process_group, window_scale,
                     with_audio, with_boot_anim):
    """Start emulator or user mode android."""

    if not self.emulator_adb_port:
      self.emulator_adb_port = portpicker.PickUnusedPort()
    if not self.emulator_telnet_port:
      self.emulator_telnet_port = portpicker.PickUnusedPort()
    if not self.device_serial:
      self.device_serial = 'localhost:%s' % self.emulator_adb_port

    emulator_binary = os.path.abspath(
        self.android_platform.GetEmulator(
            self._metadata_pb.emulator_architecture,
            self._metadata_pb.emulator_type))

    pipe_dir = self._TempDir('pipe_trav')
    exec_dir = self._SessionImagesDir()
    self._emulator_env = self._MakeEmulatorEnv(os.environ)

    if (self._metadata_pb.emulator_type in
        [emulator_meta_data_pb2.EmulatorMetaDataPb.QEMU,
         emulator_meta_data_pb2.EmulatorMetaDataPb.QEMU2]):
      self._PrepareQemuArgs(emulator_binary, net_type, window_scale,
                            with_audio, with_boot_anim)
    else:
      raise Exception('Not known emulator type %d' %
                      self._metadata_pb.emulator_type)

    logging.info('Executing: %s', self._emulator_start_args)
    timer.start(_SPAWN_EMULATOR)

    self._emulator_exec_dir = exec_dir
    self._sockets_dir = os.path.join(exec_dir, 'sockets')
    os.makedirs(self._sockets_dir)
    with contextlib.closing(
        resources.GetResourceAsFile(
            'android_test_support/'
            'tools/android/emulator/daemon/x86/pipe_traversal')) as piper:
      with open(os.path.join(pipe_dir, 'pipe_traversal'), 'w+b') as output:
        shutil.copyfileobj(piper, output)
        os.chmod(os.path.join(pipe_dir, 'pipe_traversal'), stat.S_IRWXU)

    self._child_will_delete_tmp = self.delete_temp_on_exit
    logging.info('Launching emulator in: %s', exec_dir)

    # Initialize _EmulatorLogFile here by calling it in parent process.
    # Otherwise it would be initialized in child process then we can't get
    # its name.
    with self._EmulatorLogFile('r') as f:
      logging.info('Write emulator log to %s', f.name)

    self._emu_process_pid = self._ForkWatchdog(
        new_process_group,
        self._emulator_start_args,
        self._emulator_env,
        exec_dir,
        pipe_dir)

    timer.stop(_SPAWN_EMULATOR)

    self._PollEmulatorStatus(timer)
    self.ExecOnDevice(['setprop', 'qemu.host.socket.dir',
                       str(self._sockets_dir)])
    self.ExecOnDevice(['setprop', 'qemu.host.hostname', socket.gethostname()])
    # Use IPv6 DNS server address in pure IPv6 environment.
    # fec0::3 is the default IPv6 DNS server address of qemu.
    # qemu forwards any DNS requests to this address to host DNS servers.
    if _IPv6OnlyEnv():
      self.ExecOnDevice(['setprop', 'net.eth0.dns1', 'fec0::3'])
      self.ExecOnDevice(['setprop', 'net.dns1', 'fec0::3'])
    # set screen off timeout to 30 minutes.
    self._SetDeviceSetting(self.GetApiVersion(), 'system',
                           'screen_off_timeout', '1800000')
    # disable lockscreen, this works on most api levels.
    self._SetDeviceSetting(self.GetApiVersion(), 'secure',
                           'lockscreen.disabled', '1')
    # disable software keyboard when hardware keyboard is there.
    self._SetDeviceSetting(self.GetApiVersion(), 'secure',
                           'show_ime_with_hard_keyboard', '0')

    if FLAGS.long_press_timeout:
      if self.GetApiVersion() == 10:
        logging.warn('long_press_timeout doesn\'t work on api 10.')
      else:
        self._SetDeviceSetting(self.GetApiVersion(), 'secure',
                               'long_press_timeout',
                               str(FLAGS.long_press_timeout))
    # fix possible stuck keyguardscrim window.
    self._DismissStuckKeyguardScrim()
    # ensure that processes that hang can write to /data/anr/traces.txt
    self.ExecOnDevice(['mkdir -p /data/anr && chmod -R 777 /data/anr'])
    logging.info(self.ExecOnDevice(['getprop']))
  # pylint: enable=too-many-statements

  def _ForkWatchdog(self, new_process_group, emu_args, emu_env, emu_wd,
                    pipe_dir):
    """Forks a process to launch and monitor the emulator and helpers.

    This process lives as long as the emulator process is running. Once
    the emulator exits, all temp files associated with the emulator are
    cleaned up. (These can get quite large, due to system image size).
    Also the emulator has several helper binaries which run along side it
    like pipe_traversal. This watchdog monitors and restarts them if they
    die.

    Args:
      new_process_group: spawn the emulator in a seperate session
      emu_args: the entire commandline for the emulator
      emu_env: the entire environment for the emulator
      emu_wd: the working directory to run the emulator in
      pipe_dir: the directory to find pipe_traversal binary in
    Returns:
      The PID of the watchdog process.
    """
    assert os.path.exists(emu_wd)
    assert os.path.exists(pipe_dir)
    assert os.path.exists(emu_args[0])

    fork_result = os.fork()
    if fork_result != 0:
      return fork_result
    else:
      res = self._WatchdogLoop(new_process_group, emu_args, emu_env, emu_wd,
                               pipe_dir)
      sys.stdout.flush()
      sys.stderr.flush()
      # yes _exit. "The standard way to exit is sys.exit(n). _exit() should
      # normally only be used in the child process after a fork()."
      # We do not want to run our parent's exit handlers.
      os._exit(res)  # pylint: disable=protected-access

  def _WatchdogLoop(self, new_process_group, emu_args, emu_env, emu_wd,
                    pipe_dir):
    """The main loop of the watchdog process."""
    if new_process_group:
      os.setsid()
    os.closerange(-1, subprocess.MAXFD)
    watchdog_dir = None
    if 'TEST_UNDECLARED_OUTPUTS_DIR' in os.environ:
      watchdog_dir = tempfile.mkdtemp(
          dir=os.environ['TEST_UNDECLARED_OUTPUTS_DIR'])
    else:
      watchdog_dir = self._TempDir('watchdog')

    sys.stdin = open(os.devnull)
    sys.stdout = open(os.path.join(watchdog_dir, 'watchdog.out'), 'w+b', 0)
    sys.stderr = open(os.path.join(watchdog_dir, 'watchdog.err'), 'w+b', 0)
    pipe_service_processes = self._StartPipeServices(pipe_dir)
    tn_pipe_service_process = self._StartTelnetPipeServices(pipe_dir)
    if self._display:
      self._display.Start()
      emu_env.update(self._display.environment)

    emu_process = common.Spawn(emu_args,
                               exec_env=emu_env,
                               exec_dir=emu_wd,
                               proc_input=True,
                               proc_output=self._EmulatorLogFile('wb+'))

    while True:
      logging.info('Processes launched - babysitting!')
      dead_pid, status = os.wait()
      logging.info('Dead pid: %s - status %s', dead_pid, status)
      if emu_process.pid == dead_pid:
        logging.info('Emulator has died')
        # emu has died.
        logging.info('Killing pipe services')
        for p in pipe_service_processes:
          try:
            p.terminate()
          except OSError as e:
            logging.info('Error killing services: %s - continue', e)
        logging.info('pipe services terminated')
        logging.info('Killing telnet services')
        try:
          tn_pipe_service_process.terminate()
          logging.info('telnet services terminated')
        except OSError as e:
          logging.info('Error killing services: %s - continue', e)

        if self._display:
          try:
            logging.info('Killing display: Xvfb, x11vnc, if they were started.')
            self._display.Kill()
            logging.info('Display terminated')
          except OSError as e:
            logging.info('Error killing display: %s - continue', e)

        if self.delete_temp_on_exit and self._emulator_tmp_dir:
          logging.info('Cleaning up data dirs.')
          print 'cleanup data dirs...'
          self.CleanUp()
          logging.info('Clean up done.')
        return status
      elif dead_pid in [p.pid for p in pipe_service_processes]:
        # oh noes.
        try:
          logging.info('Pipe traversal daemon died - attempting to revive')
          for p in pipe_service_processes:
            try:
              p.terminate()
            except OSError as e:
              # ignore.
              pass
          pipe_service_processes = self._StartPipeServices(pipe_dir)
          logging.info('restarted Pipe traversal daemon')
        except OSError as e:
          logging.info('Failed to restart pipe traversal daemon. %s', e)
      elif tn_pipe_service_process.pid == dead_pid:
        try:
          logging.info('Telnet daemon died - attempting to revive')
          tn_pipe_service_process = self._StartTelnetPipeServices(pipe_dir)
          logging.info('restarted telnet daemon')
        except OSError as e:
          logging.info('Failed to restart telnet daemon. %s', e)

  def _TogglePipeServices(self, on_off):
    """When we take snapshots we shut down pipe services.

    If we restore from snapshots we need to turn them back on.

    This uses standard adb to toggle them.

    Args:
      on_off: bool to turn on or off.
    Returns:
      True on success - false otherwise.
    """
    #  "No Gimli, I would not take the road through Moria unless I had no other
    #  choice.".replace('Moria', 'adb') - Gandalf
    if not self.ConnectDevice():
      return False
    action = on_off and 'start' or 'stop'
    services = ['pipe_traverse', 'tn_pipe_traverse']
    for service in services:
      try:
        common.SpawnAndWaitWithRetry(
            [self.android_platform.real_adb,
             '-s', self.device_serial,
             'wait-for-device',
             'shell', action, service],
            timeout_seconds=10,
            exec_env=self._AdbEnv())
      except common.SpawnError:
        return False

    self._pipe_traversal_running = on_off

    if not on_off:
      try:
        common.SpawnAndWaitWithRetry(
            [self.android_platform.real_adb,
             '-s', self.device_serial,
             'disconnect'],
            timeout_seconds=10,
            exec_env=self._AdbEnv())
      except common.SpawnError:
        return False
    return True

  def _StartPipeServices(self, pipe_dir):
    """Starts pipe_traversal services for the host.

    This function is intended to be ran under the process that babysits the
    emulator.

    Args:
      pipe_dir: Directory where pipe_traversal binary lives and where logs
        are stored.
    Returns:
      the pipe_traversal task.
    """
    log_path = os.path.join(pipe_dir, 'pipe.log.txt')
    test_output_dir = os.environ.get('TEST_UNDECLARED_OUTPUTS_DIR')
    if test_output_dir:
      log_path = os.path.join(test_output_dir, 'pipe.log.txt')

    logfile = open(log_path, 'a+b')
    pipe_bin = os.path.join(pipe_dir, 'pipe_traversal')
    args = [
        pipe_bin,
        '--action', 'host-service',
        '--device_serial', 'localhost:%s' % self.emulator_adb_port,
        '--emulator_dir', self._emulator_exec_dir]
    # Just run a fake pipe server on host and then adb.turbo
    # will fallback to real adb. We run a fake pipe_service here
    # so we don't need to add a few if/else branch elsewhere.
    if self._use_real_adb:
      args = ['sleep', '365d']
      return [subprocess.Popen(['sleep', '365d'])]
    else:
      pipes = []
      pipes.append(
          subprocess.Popen(
              args,
              stdin=open('/dev/null'),  # cannot use the _DEV_NULL var,
              # b/c we close all fds across forks.
              stderr=subprocess.STDOUT,
              stdout=logfile,
              cwd=self._sockets_dir,
              close_fds=True))

      svcs = ['pull-pipe', 'shell-pipe', 'push-pipe', 'port-forward-manager']
      aliases = [
          'emulator-%s' % self.emulator_telnet_port,
          '127.0.0.1:%s' % self.emulator_adb_port
      ]
      for alias in aliases:
        for svc in svcs:
          pipes.append(
              subprocess.Popen(
                  [
                      pipe_bin,
                      '--action=raw',
                      '--relay_addr',
                      'unix:@/turbo/localhost:%s/%s' % (self.emulator_adb_port,
                                                        svc),
                      '--external_addr',
                      'unix-listen:@/turbo/%s/%s' % (alias, svc),
                      '--frame_relay=false',
                  ],
                  close_fds=True,))
      return pipes

  def _StartTelnetPipeServices(self, pipe_dir):
    """Starts telnet pipe_traversal services for the host.

    Listens on sockets/qemu.mgmt and routes to the emulator's console port.

    Args:
      pipe_dir: Directory where pipe_traversal binary lives and where logs
        are stored.
    Returns:
      the pipe_traversal task.
    """
    qemu_mgmt_path = os.path.join(self._sockets_dir, 'qemu.mgmt')
    if os.path.exists(qemu_mgmt_path):
      os.remove(qemu_mgmt_path)

    log_path = os.path.join(pipe_dir, 'telnet_pipe.log.txt')
    test_output_dir = os.environ.get('TEST_UNDECLARED_OUTPUTS_DIR')
    if test_output_dir:
      log_path = os.path.join(test_output_dir, 'telnet_pipe.log.txt')

    log_file = open(log_path, 'a+b')
    pipe_bin = os.path.join(pipe_dir, 'pipe_traversal')
    args = [
        pipe_bin,
        '--action=raw',
        '--external_addr=tcp:localhost:%d' % self.emulator_telnet_port,
        '--relay_addr=unix-listen:qemu.mgmt',
        '--frame_relay']
    return subprocess.Popen(
        args,
        stdin=open('/dev/null'),
        stderr=subprocess.STDOUT,
        stdout=log_file,
        cwd=self._sockets_dir,
        close_fds=True)

  def ExecOnDevice(self, args, stdin=_DEV_NULL):
    """Execute commands on device with adb."""

    assert self._IsPipeTraversalRunning()
    assert self._CanConnect(), 'missing details to connect to adb.'
    emu_commandline = ' '.join(args)
    # Some version of adb just exit with error if the shell command
    # returns error. In such case, always make sure we get a good
    # exit status.
    if self._use_real_adb:
      emu_commandline += ' || true'
    logging.info('Executing on emulator: %s', emu_commandline)
    args = [self.android_platform.adb,
            '-s', self.device_serial,
            'shell', emu_commandline]
    logging.info('Executing on emulator: %s', args)
    proc = subprocess.Popen(args, stdin=stdin, env=self._AdbEnv(),
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    out, err = proc.communicate()
    if err:
      logging.warn('Something is wrong: %s', err)
    if proc.returncode:
      self._ShowEmulatorLog()
      raise Exception('Adb command failed, stdout:%s stderr:%s' % (out, err))
    return out

  def BroadcastDeviceReady(self, extras=None, action=_DEFAULT_BROADCAST_ACTION):
    """Sends a broadcast message to the device."""
    if not extras:
      return

    # Send with Intent.FLAG_RECEIVER_FOREGROUND (268435456) so the OS know
    # this broadcast is important to us (stands in the way of giving the user
    # the emu.
    args = ['am',
            'broadcast',
            '-a',
            action]

    # flag 0x10000000 FLAG_RECEIVER_FOREGROUND
    # flag 0x00000020 FLAG_INCLUDE_STOPPED_PACKAGES
    flag = 0x10000020
    args.extend(['-f', '%s' % flag])
    for extra_key, extra_value in extras.items():
      if isinstance(extra_value, bool):
        args.extend(['--ez', extra_key, str(extra_value).lower()])
      else:
        args.extend(['-e', extra_key, extra_value])

    # explicitly send this to our package if action equals to default action.
    # (only supported after API level 10)
    if self.GetApiVersion() > 10 and action == _DEFAULT_BROADCAST_ACTION:
      args.append(_BOOTSTRAP_PKG)
    logging.info(self.ExecOnDevice(args))

  # pylint: disable=too-many-statements
  def _PollEmulatorStatus(self, timer=None):
    """Blocks until the emulator is fully launched.

    Args:
      timer: stopwatch to measure how long each check takes

    Raises:
      Exception: if the emulator dies or doesn't become lively in a reasonable
      timeframe.
    """
    if not timer:
      timer = stopwatch.StopWatch()

    fully_booted = False
    system_server_running = False
    pm_running = False
    adb_listening = False
    adb_connected = False
    sd_card_mounted = False
    external_storage = None
    boot_complete_present = False
    launcher_started = False
    dpi_ok = False
    pipe_traversal_ready = False

    interval = self._connect_poll_interval
    max_attempts = self._connect_max_attempts

    attempter = Attempter(timer, interval)

    while not fully_booted:
      if (attempter.total_attempts > max_attempts or
          time.time() > self._time_out_time):
        self._ShowEmulatorLog()
        if adb_listening:
          log = self.ExecOnDevice(['logcat', '-v', 'threadtime', '-d'])
          logging.info('Android logcat below ' + '=' * 50 + '\n%s', log)
          logging.info('Android logcat end ' + '=' * 50)
        raise Exception('Haven\'t been able to connect to device after %s'
                        ' attempts.' % attempter.total_attempts)

      logging.info('system: %s pm: %s adb: %s sdcard: %s '
                   'boot_complete: %s launcher: %s pipes: %s '
                   'current step attempts: %s total attempts: %s',
                   system_server_running,
                   pm_running,
                   adb_listening,
                   sd_card_mounted,
                   boot_complete_present,
                   launcher_started,
                   pipe_traversal_ready,
                   attempter.step_attempts,
                   attempter.total_attempts)
      self._EnsureEmuRunning()

      if not adb_listening:
        adb_listening = attempter.AttemptStep(self._AdbListeningStep,
                                              'Checking if adb is listening.',
                                              _ADB_LISTENING_CHECK,
                                              _ADB_LISTENING_CHECK_FAIL_SLEEP)
        if not adb_listening:
          continue

      if not adb_connected and self._use_real_adb:
        self.ConnectDevice()
        wait_args = [self.android_platform.real_adb, '-s',
                     'localhost:%s' % self.emulator_adb_port, 'wait-for-device']
        common.SpawnAndWaitWithRetry(wait_args, retries=0, timeout_seconds=30,
                                     exec_env=self._AdbEnv())
        adb_connected = True

      if not pipe_traversal_ready:
        pipe_traversal_ready = attempter.AttemptStep(
            self._PipeTraversalRestoreStep,
            'Checking Pipe Traversal.',
            _PIPE_TRAVERSAL_CHECK,
            _PIPE_TRAVERSAL_CHECK_FAIL_SLEEP)
        if not pipe_traversal_ready:
          continue
        self.EnableLogcat()

      self._DetectFSErrors()

      if not system_server_running:
        system_server_running = attempter.AttemptStep(
            self._CheckSystemServerProcess,
            'Checking System Server',
            _SYS_SERVER_CHECK,
            _SYS_SERVER_CHECK_FAIL_SLEEP)
        if not system_server_running:
          continue

      self._KillCrashedProcesses()

      if not pm_running:
        pm_running = attempter.AttemptStep(self._CheckPackageManagerRunning,
                                           'Checking package manager',
                                           _PM_CHECK,
                                           _PM_CHECK_FAIL_SLEEP)
        if not pm_running:
          continue

      if not sd_card_mounted:
        if not external_storage:
          external_storage = ('%s %s %s' % (
              self._GetEnvironmentVar('EMULATED_STORAGE_SOURCE'),
              self._GetEnvironmentVar('EXTERNAL_STORAGE'),
              self._GetEnvironmentVar('ANDROID_STORAGE'))).split()

        def _ExternalStorageReady():
          return external_storage and self._CheckMount(external_storage)
        sd_card_mounted = attempter.AttemptStep(_ExternalStorageReady,
                                                'Checking external storage',
                                                _SD_CARD_MOUNT_CHECK,
                                                _SD_CARD_MOUNT_CHECK_FAIL_SLEEP)
        if not sd_card_mounted:
          perc_steps_spent = float(
              attempter.step_attempts) / max_attempts
          perc_steps_spent *= 100
          if perc_steps_spent > 20:
            self._TransientDeath('SDCard mount issues. This is a transient KI.')
          continue

      if not boot_complete_present:
        boot_complete_present = attempter.AttemptStep(
            self._CheckBootComplete,
            'Checking for boot complete',
            _BOOT_COMPLETE_PRESENT,
            _BOOT_COMPLETE_FAIL_SLEEP)
        if not boot_complete_present:
          continue

      if not dpi_ok:
        if not attempter.step_attempts:
          if not self.IsInstalled(_BOOTSTRAP_PKG):
            self.InstallApk(resources.GetResourceFilename(_BOOTSTRAP_PATH))

        self._UnlockScreen()
        dpi_ok = attempter.AttemptStep(self._CheckDpi,
                                       'Checking DPI',
                                       _CHECK_DPI,
                                       _CHECK_DPI_FAIL_SLEEP)
        if not dpi_ok:
          if attempter.step_attempts > 4:
            self._TransientDeath('Haven\'t been able to read correct DPI values'
                                 ' in %s attempts.' % attempter.step_attempts)
          continue

      if not launcher_started:
        if attempter.step_attempts > 0:
          self._UnlockScreen()

        if attempter.step_attempts > 2 and not self._kicked_launcher:
          # sometimes the handoff to start the launcher fails. doing
          # am start -a android.intent.action.MAIN \
          # -c android.intent.category.HOME can't hurt.
          self._KickLauncher()
        launcher_started = attempter.AttemptStep(self._CheckLauncherStarted,
                                                 'Checking launcher app.',
                                                 _LAUNCHER_STARTED,
                                                 _LAUNCHER_STARTED_FAIL_SLEEP)
        if not launcher_started:
          continue
      fully_booted = True

    self._running = True
    self._KillCrashedProcesses()
  # pylint: enable=too-many-statements

  def _KillCrashedProcesses(self):
    """Kills processes which have crashed or ANR but have not fully died."""

    # usually our g3_monitor will kill these processes for us, but there is
    # a very brief time before it starts running where a proc could crash
    # and not get cleaned up.
    event_logs = self.ExecOnDevice([
        'logcat',
        '-d',
        '-b',
        'events',
        '-s',
        'am_crash:*',
        'am_anr:*',
        'am_proc_died:*'])

    procs_to_kill = self._FindProcsToKill(event_logs)
    if procs_to_kill:
      self.ExecOnDevice(['kill'] + procs_to_kill)

  def _FindProcsToKill(self, event_logs):
    """Given a set of event logs, creates a list of pids to kill."""
    dead_procs = set()
    crashed_or_anr_procs = set()

    for entry in re.finditer(_ANR_RE, event_logs):
      pid = None
      tag, log_message = entry.groups()
      # treats the first pid like string in the log as the process which
      # has crashed or ANR'd. Although the format of the log_message has
      # changed - the fact that the 1st pid like number being the bad
      # proc has not changed.
      for message in log_message.split(','):
        message = message.strip()
        if message.isdigit():
          maybe_pid = int(message)
          if maybe_pid > 0 and maybe_pid < 32768:
            pid = maybe_pid
            break

      if pid:
        if tag == 'am_proc_died':
          dead_procs.add(pid)
        else:
          crashed_or_anr_procs.add(pid)
      else:
        logging.warn('Could not interpret crash record: %s', entry.group(0))

    return [str(x) for x in crashed_or_anr_procs if x not in dead_procs]

  def _GetEnvironmentVar(self, varname):
    """Return the value of a environment variable.

    Args:
      varname: name of the environment variable.

    Returns:
      Value of the environment variable or None if the variable is not defined.
    """
    return self.ExecOnDevice(['printenv', varname]).strip()

  def _CheckBootComplete(self):
    output = self.ExecOnDevice(['getprop'])
    completed = 'dev.bootcomplete' in output
    if not completed:
      completed = 'sys.boot_completed' in output
    return completed

  def _CheckMount(self, mount_points):
    output = self.ExecOnDevice(['mount'])
    mounted = [mount_point for mount_point in mount_points
               if mount_point in output]
    if not mounted:
      logging.info('%s not mounted - mount info: %s', mount_points, output)
    logging.info('mounted: %s', mounted)
    return mounted

  def _Remount(self, mount_point, permission):
    mount_cmd = ['mount', '-o', 'remount,%s' % permission, mount_point]
    if self.GetApiVersion() <= 10:
      mount_cmd.append(mount_point)
    self.ExecOnDevice(mount_cmd)

  def _DetermineArchitecture(self, source_properties):
    if SYSTEM_ABI_KEY in source_properties:
      return source_properties[SYSTEM_ABI_KEY].lower()
    else:
      raise Exception('Missing %s in %s' % (SYSTEM_ABI_KEY, source_properties))

  def _DetermineSupportedDrivers(self, source_properties):
    """Return a list of supported gl drivers for this device."""

    drivers = [NO_OPEN_GL]
    if self._SupportsGPU(source_properties):
      drivers.append(HOST_OPEN_GL)
      drivers.append(MESA_OPEN_GL)
      drivers.append(SWIFTSHADER_OPEN_GL)
    api_level = int(source_properties[API_LEVEL_KEY])
    # Currently only api 19+ has guest mode GL support.
    if api_level >= 19:
      drivers.append(GUEST_OPEN_GL)
    return drivers

  def _DetermineSensitiveImage(self, source_properties):
    return source_properties.get(SENSITIVE_SYSTEM_IMAGE, False)

  def _SupportsGPU(self, source_properties):
    arch = self._DetermineArchitecture(source_properties)
    # All x86 images support GPU now.
    if 'x86' == arch:
      return True
    api_level = int(source_properties[API_LEVEL_KEY])
    if api_level > 10:
      return True
    else:
      return False

  def _DetermineQemuArgs(self, source_properties, kvm_present):
    """Return a list of qemu arguments based on architecture and kvm setting."""

    arch = self._DetermineArchitecture(source_properties)
    qemu_args = []
    if 'x86' == arch:
      if kvm_present:
        qemu_args.append('-enable-kvm')
        if (not self._metadata_pb or self._metadata_pb.emulator_type ==
            emulator_meta_data_pb2.EmulatorMetaDataPb.QEMU):
          qemu_args.extend(['-append', 'nopat'])
        # TODO: pass an appropriate value for -cpu when
        # KVM snapshot migrations are a reality (again.)
      else:
        qemu_args.append('-disable-kvm')
    if 'armeabi-v7a' == arch:
      qemu_args += ['-cpu', 'cortex-a8']
    return qemu_args

  def _WithKvm(self, source_properties, kvm_present):
    arch = self._DetermineArchitecture(source_properties)
    return 'x86' == arch and kvm_present

  def Ping(self):
    if self._CanConnect():
      return (self._CheckSystemServerProcess() and
              self._CheckPackageManagerRunning())
    else:
      return False

  def GetEmulatorMetadataProto(self):
    assert self._metadata_pb, 'Emulator not started.'
    return self._metadata_pb

  def CleanUp(self):
    if self._emulator_tmp_dir and os.path.exists(self._emulator_tmp_dir):
      shutil.rmtree(self._emulator_tmp_dir, ignore_errors=True)

  def _DismissStuckKeyguardScrim(self):
    """Detect and dismiss possible stuck keyguardScrim window."""

    # We've only seen this bug on api level 21+
    if self.GetApiVersion() < 21:
      return
    count = 0
    while count < 5:
      res = self.ExecOnDevice(['dumpsys', 'input'])
      match = re.search('FocusedWindow: name=.*KeyguardScrim', res)
      if not match:
        return
      count += 1
      time.sleep(2)
    # The Android bug (at least for api level 22) here is:
    # showScrim and hideScrim in KeyguardServiceDelegate.java run in different
    # threads and post actual Runnable in UI thread of mScrim View.
    # When booting, they both are called in systemBooted at
    # PhoneWindowManager.java, so even showScrim was called before hideScrim,
    # Runnable posted by showScrim could run after Runnable posted by
    # hideScrim and then leave home screen in weird status.
    # Here we send power key events which shuts off screen first and then lights
    # on screen. This make sure Runnable posted by hideScrim runs at last.
    send_power_key = ['input', 'keyevent', '26']
    self.ExecOnDevice(send_power_key)
    time.sleep(3)
    self.ExecOnDevice(send_power_key)
    logging.info('Send power key events to dismiss KeyguardScrim')

  def _CheckLauncherStarted(self):
    """Checks if Launcher is started."""

    if self.GetApiVersion() < 21:
      logging.info('checking event buffer for launcher...')
      command_line = [
          'logcat',
          '-d',
          '-b',
          'events',
          '-s',
          'activity_launch_time:*',  # eclair to JB
          'am_activity_launch_time:*',  # JB-MR-1 +
          'am_on_resume_called:*',  # API 23 +
      ]
    else:
      logging.info('checking process list for launcher...')
      command_line = ['ps']

    output = self.ExecOnDevice(command_line)
    launcher_started = ('com.android.launcher' in output or
                        'com.google.android.wearable' in output or
                        'com.google.glass.nowtown' in output or
                        'com.google.android.googlequicksearchbox' in output or
                        'com.google.android.tv' in output or
                        'com.android.tv' in output)
    logging.info('launcher running? %s', launcher_started)
    return launcher_started

  def _AdbListeningStep(self):
    try:
      s = socket.create_connection(('localhost', int(self.emulator_adb_port)))
      s.close()
      return True
    except socket.error:
      return False

  def _PipeTraversalRestoreStep(self):
    if self._IsPipeTraversalRunning():
      return True
    else:
      return self._TogglePipeServices(True)

  def _KickLauncher(self):
    """Kicks off launcher start."""
    logging.info('kicking launcher...')
    self.ExecOnDevice([
        'am',
        'start',
        '-a',
        'android.intent.action.MAIN',
        '-c',
        'android.intent.category.HOME'])

    self._kicked_launcher = True

  def _CheckSystemServerProcess(self):
    output = self.ExecOnDevice(['ps'])

    system_server_running = 'system_server' in output
    logging.info('system_server running? %s', system_server_running)
    return system_server_running

  def _CheckPackageManagerRunning(self):
    output = self.ExecOnDevice(['pm', 'path', 'android'])

    pm_running = 'package:' in output
    logging.info('pm running? %s', pm_running)
    return pm_running

  def _EmulatorLogFile(self, mode):
    if not self._emulator_log_file:
      with tempfile.NamedTemporaryFile(
          prefix='emulator_log',
          suffix='.txt',
          dir=os.environ.get('TEST_UNDECLARED_OUTPUTS_DIR'),
          delete=False) as f:
        self._emulator_log_file = f.name
    return open(self._emulator_log_file, mode)

  def ConnectDevice(self):
    """Connects the device to the adb server.

    Returns:
      True on Success, False otherwise.
    """
    assert self._CanConnect()
    if FLAGS.skip_connect_device:
      return True
    connect_args = [self.android_platform.real_adb,
                    'connect',
                    'localhost:%s' % self.emulator_adb_port]
    logging.info('Connecting adb server to device: %s', connect_args)
    connect_task = None
    if not self.adb_server_port:
      self.adb_server_port = portpicker.PickUnusedPort()
    elif self.adb_server_port < 0 or self.adb_server_port > 65535:
      logging.warn('Invalid adb server port %d, skip connecting',
                   self.adb_server_port)
      return

    try:
      logging.info('Starting: %s', connect_args)
      connect_task = common.SpawnAndWaitWithRetry(
          connect_args,
          proc_output=True,
          exec_env=self._AdbEnv(),
          timeout_seconds=ADB_SHORT_TIMEOUT_SECONDS,
          retries=5)
      logging.info('Done: %s', connect_args)
    except common.SpawnError:
      return False

    connect_stdout = connect_task.borg_out
    logging.info('Status: %s ', connect_stdout)
    odd_successful_connection = 'localhost:%s:%s' % (self.emulator_adb_port,
                                                     self.emulator_adb_port)
    return ('connected' in connect_stdout or
            odd_successful_connection in connect_stdout)

  def _AdbEnv(self):
    """Prepare environment for running adb."""

    env = {}
    if not self.adb_server_port:
      self.adb_server_port = portpicker.PickUnusedPort()
    if not self.device_serial:
      self.device_serial = 'localhost:%s' % self.emulator_adb_port
    env['ANDROID_ADB_SERVER_PORT'] = str(self.adb_server_port)
    env['ANDROID_ADB'] = self.android_platform.real_adb
    env['ANDROID_SERIAL'] = self.device_serial
    if self._emulator_env and 'HOME' in self._emulator_env:
      env['HOME'] = self._emulator_env['HOME']
    test_output_dir = os.environ.get('TEST_UNDECLARED_OUTPUTS_DIR')
    if test_output_dir:
      env['TMPDIR'] = test_output_dir
    return env

  def _SetDeviceSetting(self, api_level, table, name, value):
    """Set device settings."""

    if api_level < 16:
      sql_cmd = (
          '"INSERT OR REPLACE INTO %s (name, value) VALUES (\'%s\', %s);"' %
          (table, name, value))
      cmd = ['sqlite3', _DB_PATH, sql_cmd]
    elif api_level == 16:
      cmd = [
          'content insert --uri content://settings/%s --bind name:s:%s '
          '--bind value:s:%s' % (table, name, value)]
    else:
      cmd = ['settings put %s %s %s' % (table, name, value)]
    self.ExecOnDevice(cmd)

  def _GetDeviceSetting(self, table, name):
    sql_cmd = '"SELECT value FROM %s WHERE name=\'%s\';"' % (table, name)
    return self.ExecOnDevice(['sqlite3', _DB_PATH, sql_cmd])

  def _DisableSideloading(self):
    api = self.GetApiVersion()
    if api in [17, 19]:
      db_table = 'global'
    else:
      db_table = 'secure'

    self._SetDeviceSetting(api, db_table, 'install_non_market_apps', 0)
    self.ExecOnDevice(['pm', 'disable com.android.providers.settings'])

  def _RemoveSettingsControl(self):
    """Remove SettingsControl App from system."""
    self._Remount('/system', 'rw')

    if self.GetApiVersion() < 19:
      app_dir = '/system/app'
      odex_dir = app_dir
    elif self.GetApiVersion() == 19:
      app_dir = '/system/priv-app'
      odex_dir = app_dir
    else:
      app_dir = '/system/priv-app/Settings'
      arch = self._metadata_pb.emulator_architecture
      if arch.startswith('arm'):
        arch = 'arm'
      odex_dir = os.path.join(app_dir, 'oat/%s' % arch)

    self.ExecOnDevice(['rm', os.path.join(app_dir, 'Settings.apk')])
    self.ExecOnDevice(['rm', os.path.join(odex_dir, 'Settings.odex')])
    self._Remount('/system', 'ro')

  def _RemoveAdbd(self):
    self._Remount('/', 'rw')
    self.ExecOnDevice(['rm', '/sbin/adbd'])
    self.ExecOnDevice(['stop', 'adbd'])
    self._Remount('/', 'ro')

  def Lockdown(self, lockdown_level):
    if lockdown_level == 'no_settings_control':
      self._DisableSideloading()
      self._RemoveSettingsControl()

    if lockdown_level == 'no_settings_control' or lockdown_level == 'no_adb':
      self._RemoveAdbd()

  def _QueryServices(self):
    """Returns a dictionary of all the init services and their states.

    Android init stores init service state in the property system. It prefixes
    its keys with init.svc.SERVICE NAME and the values can be stopped, running
    and restarting.

    Returns:
      a dictionary of service name to state.
    """
    init_prop_header = '[init.svc.'
    props = self.ExecOnDevice(['getprop'])
    # output looks roughly like:
    # [someprop]: [its value]\n
    # [otherprop]: [val 2]\n
    # [init.svc.vold]: [running]\n
    # [anotherprop]: [val 3]\n

    init_svcs = [s.split(':') for s in props.splitlines()
                 if s.startswith(init_prop_header)]
    cleaned_key_vals = [(k[len(init_prop_header):-1], v.strip()[1:-1])
                        for k, v in init_svcs]
    return dict(cleaned_key_vals)

  def _DetectFSErrors(self):
    """Detects the rare situation that /data or /cache have become corrupt.

    With ext4 FS on Android L we very occasionally (0.1% time) see an ext4-fs
    error that causes the filesystem to be remounted ro. This happens AFTER
    init had already succeeded mounting it rw.

    Raises:
      TransientEmulatorFailure: if corruption is detected.
    """
    fs_opts = [point.split()[3] for point
               in self.ExecOnDevice(['mount']).splitlines()
               if '/cache' in point or '/data' in point]
    if [opt for opt in fs_opts if 'ro' in opt.split(',')]:
      self._TransientDeath('RW file system has been remounted RO!')

  def _CleanUmount(self, mount_point):
    """Attempts to cleanly umount a particular mount point.

    Executes sync commands on the emulator, remounts the mount point
    read only and then attempts to umount the mount point.

    If the mount point is an ext4 filesystem - e2fsck is performed on the
    backing device after it is dismounted.

    Args:
      mount_point: the path on the filesystem to dismount

    Returns:
      True if the path was dismounted and if all fsck checks succeed.
    """
    self.ExecOnDevice(['sync', '&&', 'sync'])
    self._Remount(mount_point, 'ro')
    info = [point for point in self.ExecOnDevice(['mount']).splitlines()
            if mount_point in point.split()]
    umount_attempts = 0
    umounted = False
    while umount_attempts < 5 and not umounted:
      umount_attempts += 1
      self.ExecOnDevice(['umount', mount_point])
      umounted = mount_point not in self.ExecOnDevice(['mount'])
      if not umounted:
        time.sleep(self._connect_poll_interval)

    if not umounted:
      err = self.ExecOnDevice(['umount', mount_point])
      logging.warn('%s: Could not be umounted. Error: %s', mount_point, err)

    # e2fsck is not present on API < 21
    if self.GetApiVersion() < 21:
      return umounted

    clean = False
    if info:
      info = info[0]
      if 'ext4' in info:
        dev = info.split()[0]
        fsck_out = self.ExecOnDevice(['e2fsck', '-v', '-f', '-p', dev])
        if 'UNEXPECTED INCONSISTENCY' in fsck_out:
          logging.error('%s: FS Corruption! %s', mount_point, fsck_out)
        else:
          clean = True
      else:
        clean = True
    else:
      logging.warn('%s: Could not retrieve mount info - cannot fsck.',
                   mount_point)
    return clean and umounted

  def _CheckLeftProcess(self):
    """Check left process on device, also killing known dead process body."""

    ps_out = self.ExecOnDevice(['ps'])
    lines = ps_out.split('\n')
    raw_key = lines[0].strip().split()
    # The output of ps is buggy on Android device.
    # The columns of header is inconsistent with
    # following lines. Just use the first 4 columns
    # and the last column.
    key = raw_key[:4] + raw_key[-1:]
    processes = []
    for l in lines[1:]:
      line = l.strip()
      if not line:
        continue
      raw_value = line.split()
      value = raw_value[:4] + raw_value[-1:]
      proc = dict(zip(key, value))
      # Ignore kernel process
      vsize = proc.get('VSIZE') or proc.get('VSZ')
      if vsize == '0':
        continue
      # Ignore init
      if proc['PID'] == '1':
        continue
      if os.path.basename(proc['NAME']) == 'pipe_traversal':
        pipe_traversal_pid = proc['PID']
        continue
      if proc['NAME'] == 'ps':
        continue
      processes.append(proc)

    suspicous = False
    for proc in processes:
      # Kill crashed "pm install" body. Its parent
      # process should be pipe_traversal.
      if (proc['PPID'] == pipe_traversal_pid and
          proc['NAME'] == 'app_process'):
        self.ExecOnDevice(['kill', '-9', proc['PID']])
        continue
      suspicous = True

    if suspicous:
      logging.warning('Some process is still running: %s\n', ps_out)

  def KillEmulator(self, politely=False):
    """Stops the emulator.

    Args:
      politely: [optional] if true we do our best to ensure the emulator exits
        cleanly.
    """
    clean_death = True
    if politely and self._vm_running:
      idle = IdleStatus(device=self)
      # Wait for system being idle.
      for _ in range(20):
        time.sleep(6)
        load = idle.RecentMaxLoad(15)
        if load < 0.1:
          break

      self.ExecOnDevice(['stop'])

      # stop any other processes that are not protected.
      running_svcs = [k for k, v in self._QueryServices().items()
                      if k not in SHUTDOWN_PROTECTED_SERVICES
                      and v != 'stopped']
      for svc in running_svcs:
        self.ExecOnDevice(['stop', svc])

      self._CheckLeftProcess()
      clean_death = self._CleanUmount('/data') and clean_death
      clean_death = self._CleanUmount('/cache') and clean_death
    telnet = self._ConnectToEmulatorConsole()
    telnet.write('kill\n')
    telnet.read_all()
    self._running = False

    if politely and not clean_death:
      self._TransientDeath('Could not cleanly shutdown emulator', False)

  def _TransientDeath(self, msg, needs_kill=True):
    if needs_kill:
      self.KillEmulator(politely=False)
    self.CleanUp()
    raise TransientEmulatorFailure(msg)

  def StoreAndCompressUserdata(self, location):
    """Stores the emulator's userdata files."""
    assert not self._running, 'Emulator is still running.'
    assert self._images_dir, 'Emulator never started.'
    assert not self._child_will_delete_tmp, 'Emulator is deleting tmp dir.'

    if not os.path.exists(os.path.dirname(location)):
      os.makedirs(os.path.dirname(location))
    logging.info('Storing emulator state to: %s', location)

    image_files = [
        self._UserdataQemuFile() + self._PossibleImgSuffix(),
        self._CacheFile() + self._PossibleImgSuffix(),
        self._SdcardFile() + self._PossibleImgSuffix(),
        self._SnapshotFile(),
        self._RamdiskFile()]

    if (self._metadata_pb.emulator_type ==
        emulator_meta_data_pb2.EmulatorMetaDataPb.QEMU2):
      image_files.append(
          os.path.join(self._SessionImagesDir(), 'version_num.cache'))

    image_files = ['./%s' % os.path.relpath(f, self._images_dir)
                   for f in image_files]

    with open(location, 'w') as dat_file:
      tar_proc = subprocess.Popen(
          ['tar', '-cSp', '-C', self._images_dir] + image_files,
          stdout=subprocess.PIPE)
      # consider replacing with pigz
      gz_proc = subprocess.Popen(
          ['gzip'],
          stdin=tar_proc.stdout,
          stdout=dat_file)
      tar_proc.stdout.close()  # tar will get a SIGPIPE if gz dies.
      gz_ret = gz_proc.wait()
      tar_ret = tar_proc.wait()
      assert gz_ret == 0 and tar_ret == 0, 'gz: %d tar: %d' % (gz_ret, tar_ret)
      logging.info('Tar/gz pipeline completes.')

  def _GetAuthToken(self, reply):
    match = re.search(r'\'(\S*\.emulator_auth_token)\'', reply)
    assert match, 'can not find file name in ' + reply
    assert os.path.exists(match.group(1)), 'can not open file ' + match.group(1)
    with open(match.group(1), 'rb') as f:
      return f.read().strip()

  def _TryAuth(self, sock):
    reply = sock.read_until('OK', 1.0)
    assert 'OK' in reply, 'connect failed, got: ' + reply
    if '.emulator_auth_token' in reply:
      sock.write('auth %s\n' % self._GetAuthToken(reply))
      reply = sock.read_until('\n', 1.0)
      assert 'OK' in reply, 'auth failed, got: ' + reply

  def _ConnectToEmulatorConsole(self):
    """Connect to Emulator console."""
    assert self._CanConnect(), 'missing details to connect to emulator.'
    attempts = 0
    while attempts < 5:
      try:
        sock = telnetlib.Telnet('localhost', self.emulator_telnet_port, 60)
        self._TryAuth(sock)
        return sock

      except socket.error as e:
        if e.errno == 111:
          # not bound yet!
          logging.info('Emulator console port not bound yet.')
          time.sleep(2)
          attempts += 1
        else:
          raise e
    raise Exception('Tried %s times to connect to emu console.' % attempts)

  def _SnapshotPresent(self):
    """Returns the avd config property snapshot.present."""
    for prop in self._metadata_pb.avd_config_property:
      if prop.name == 'snapshot.present':
        return prop
    return self._metadata_pb.avd_config_property.add(
        name='snapshot.present',
        value='no')

  def TakeSnapshot(self, name='default-boot'):
    """Take a avd snapshot."""
    self._SnapshotPresent().value = 'True'
    if not self._TogglePipeServices(False):
      time.sleep(1)
      if not self._TogglePipeServices(False):
        logging.error('Could not toggle pipe services - snapshot maybe broken.')

    telnet = self._ConnectToEmulatorConsole()
    telnet.write('avd stop\n')
    telnet.write('avd snapshot save %s\n' % name)
    telnet.write('exit\n')
    telnet.read_all()
    self._vm_running = False

  def _LoadVm(self, name='default-boot'):
    telnet = self._ConnectToEmulatorConsole()
    telnet.write('avd stop\n')
    telnet.write('avd snapshot load %s\n' % name)
    telnet.write('avd start\n')
    telnet.write('exit\n')
    telnet.read_all()

  def DeleteSnapshot(self, name='default-boot'):
    telnet = self._ConnectToEmulatorConsole()
    telnet.write('avd snapshot del %s\n' % name)
    telnet.write('exit\n')
    telnet.read_all()

  def _CyberVillainsCert(self):
    return resources.GetResourceFilename(
        os.path.join(self.android_platform.android_sdk,
                     'tools/lib/pc-bios/cybervillainsCA.cer'))

  def InstallCyberVillainsCert(self):
    """Installs a cybervillainsCA cert certificate to device."""
    api = self.GetApiVersion()
    if api < 14:
      cert_file = resources.GetResourceFilename(
          os.path.join(self.android_platform.android_sdk,
                       'tools/lib/pc-bios/%s-%s-cacerts.bks' % (
                           api, self._metadata_pb.emulator_architecture[:3])))
      destination_file = '/system/etc/security/cacerts.bks'
      self._Push(cert_file, destination_file)
    else:
      self.AddCert(self._CyberVillainsCert(), cert_format=X509.FORMAT_DER)

  def GetCopyCmd(self, file_list):
    cmd_list = []
    for f in file_list:
      src = f[0]
      dst = f[1]
      dst_dir = os.path.dirname(dst)
      dst_name = os.path.basename(dst)
      cmd_list += ['cd %s\nwrite %s %s' % (dst_dir, src, dst_name)]
    return cmd_list

  def GetInstallCertCmd(self):
    """Installs a cybervillainsCA cert certificate to system image."""
    cert_file = self._CyberVillainsCert()
    cert_name = self._GetCertName(cert_file, cert_format=X509.FORMAT_DER)
    cp_list = [[cert_file, '/etc/security/cacerts/%s' % cert_name]]
    return self.GetCopyCmd(cp_list)

  def ExecDebugfsCmd(self, image_file, cmd_list):
    """Execute debugfs commands from cmd_list on disk image file."""
    assert not self._emu_process_pid, 'Emulator is running!'
    assert 'ext4' in subprocess.check_output(['file', image_file]), (
        'Not ext4 image')
    assert os.path.exists('/sbin/debugfs'), 'No debugfs tool find'
    os.chmod(image_file, stat.S_IRWXU)
    proc = subprocess.Popen(['/sbin/debugfs', '-w', '-f', '-', image_file],
                            stdin=subprocess.PIPE)
    proc.communicate('\n'.join(cmd_list) + '\n')
    proc.wait()

  def InstallSystemApks(self, apk_paths):
    """Installs a given apk to the system partition."""
    for apk in apk_paths:
      assert os.path.exists(apk), 'apk doesnt exist at: %s' % apk
    for apk in apk_paths:
      if self.GetApiVersion() < 19:
        self._Push(apk, '/system/app/%s' % os.path.basename(apk))
      else:
        self._Push(apk, '/system/priv-app/%s' % os.path.basename(apk))
    if self.GetApiVersion() >= 21:
      self._RestartAndroid()
      self._PollEmulatorStatus()
      self._UnlockScreen()

  def _Push(self, file_path, device_path):
    """Pushes given file to device."""
    assert os.path.exists(file_path), 'file doesnt exist at: %s' % file_path
    assert os.path.isabs(device_path), 'need device absolute path'
    file_path = os.path.abspath(file_path)

    if device_path.startswith('/system'):
      self._Remount('/system', 'rw')
    logging.info('pushing: %s to %s', file_path, device_path)
    subprocess.check_call([
        self.android_platform.adb,
        '-s', self.device_serial,
        'push',
        file_path,
        device_path], env=self._AdbEnv())
    if device_path.startswith('/system'):
      self._Remount('/system', 'ro')

  def GetApiVersion(self):
    return int(self._metadata_pb.api_name)

  def HasNativeMultiDex(self):
    return self.GetApiVersion() >= 21

  def IsInstalled(self, app_id):
    return app_id in self.ExecOnDevice(['pm', 'list', 'packages', app_id])

  def _IsPermanentInstallError(self, info):
    for error in PERMANENT_INSTALL_ERROR:
      if error in info:
        return True
    return False

  def InstallApk(self, apk_path, max_tries=5, grant_runtime_permissions=False):
    """Installs the given apk onto the device."""
    assert os.path.exists(apk_path), 'apk doesnt exist at: %s' % apk_path
    attempts = 0
    install_args = [self.android_platform.adb,
                    '-s',
                    self.device_serial,
                    'install']

    # allow downgrades if api supports it.
    if self.GetApiVersion() > 20:
      install_args.append('-d')
    if self.GetApiVersion() >= 23 and grant_runtime_permissions:
      install_args.append('-g')

    install_args.extend(['-r', apk_path])

    pkg_size = os.path.getsize(apk_path)

    install_timeout_secs = 60

    uses_art = self.GetApiVersion() > 20

    if pkg_size > (30 << 20):
      # pkg more than 30mb, extend timeout.
      # TODO: Reduce to previous value (180) once GMM timeouts are
      # under control.
      install_timeout_secs = 240
    elif uses_art and pkg_size > (20 << 20):
      # if pkg more than 20mb, increase by a smaller amount
      # this is especially needed on API >= 21, where presumably art
      # optimization will add an additional time penalty
      install_timeout_secs = 120


    while True:
      logging.info('installing: %s', apk_path)
      install_output = ''
      try:
        if uses_art:
          install_output = self._Dex2OatCheckingInstall(install_args)
        else:
          install_task = common.SpawnAndWaitWithRetry(
              install_args,
              timeout_seconds=install_timeout_secs,
              exec_env=self._AdbEnv(),
              proc_output=True)
          install_output = install_task.borg_out

        if 'Success' in install_output:
          logging.info('install done: %s', apk_path)
          return
        if self._IsPermanentInstallError(install_output):
          logging.warning('install failed: %s %s', apk_path, install_output)
          raise Exception('permanent install failure')
        else:
          logging.info('Install failed: %s', install_output)
          logging.info('logcat: %s', self.ExecOnDevice(
              ['logcat -v threadtime -b all -d']))
      except common.SpawnError:
        install_output = 'timeout failure'
      attempts += 1
      assert attempts < max_tries
      logging.info('%s: attempting install again due to: %s',
                   apk_path, install_output)
      time.sleep(1)

  def _Dex2OatCheckingInstall(self, install_args):
    """Installs an apk on an ART device.

    We determine if the install failed / froze if after a period of time
    the install command has not returned and device is idle.

    Arguments:
      install_args: the installation args to pass to adb.

    Returns:
      the output of the install command (string)
    """
    install_proc = subprocess.Popen(
        install_args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=self._AdbEnv(),
        close_fds=True)
    stdout = []
    def _StdoutReader(fd, buf):
      buf.append(fd.read())

    stdout_thread = threading.Thread(
        target=_StdoutReader,
        args=(install_proc.stdout, stdout))
    stdout_thread.setDaemon(True)
    stdout_thread.start()

    poll_checks = 0
    idle = IdleStatus(device=self)

    while install_proc.poll() is None:
      time.sleep(.5)
      poll_checks += 1
      if (poll_checks % 16) != 0:
        continue
      load = idle.RecentMaxLoad(20)
      if load > 0.1:
        logging.info('system load is %f, still busy', load)
      else:
        logging.info('system load is %f for more than 30 seconds', load)
        # system is idle now, give it one last shot to tell us the
        # install has completed.
        install_proc.kill()
        install_proc.wait()
        stdout_thread.join()
        return 'after ~30s system idle, system hung? stdout: %s' % (
            stdout[0])
    logging.info('install [%s]: return code: %s', install_args,
                 install_proc.poll())
    stdout_thread.join()
    return stdout[0]

  def SyncTime(self):
    """Sync time of the emulator with host time."""
    logging.info('Syncing time.')
    if self.GetApiVersion() < 23:
      output = self.ExecOnDevice([
          'date',
          '-s',
          time.strftime('%Y%m%d.%H%M%S', time.localtime())])
    else:
      output = self.ExecOnDevice([
          'date',
          time.strftime('%m%d%H%M%Y.%S', time.localtime())])

    logging.info('Time set to: %s', output)
    return True

  def _TempDir(self, dirname):
    assert self._emulator_tmp_dir, 'Base temp dir not set!'
    assert dirname, 'No dirname'
    full_path = os.path.join(self._emulator_tmp_dir, dirname)
    assert not os.path.exists(full_path), '%s: already created!' % dirname
    os.makedirs(full_path)
    return full_path

  def LogToDevice(self, message):
    """Writes message to log."""
    logging.info('logging to logcat: %s', message)
    self.ExecOnDevice([
        'log',
        '-p',
        'i',
        '-t',
        'emulated_device',
        message])

  def RunScript(self, adb_script_path, output_file_path):
    """Executes commands against the phone's root shell.

    Please avoid relying on this function - even with relatively trivial scripts
    it's flaky about 5% of the time since we often need to kill and restart adb
    and the adb server. Strongly consider making your interactions with the
    emulator a first class method here. And ensure there are integration tests
    to cover your method and that it is idempotent and can survive having to
    kill and restart adb.

    Args:
      adb_script_path: path to file with commands to run.
      output_file_path: path to a logfile to collect output in.
    """
    assert os.path.exists(adb_script_path), 'No file at %s' % adb_script_path
    shell_args = [self.android_platform.adb]
    shell_args.append('shell')
    output_file = open(output_file_path, 'w')
    shell_task = common.Spawn(
        shell_args,
        proc_output=output_file,
        proc_input=True,
        exec_env=self._AdbEnv())
    shell_task.communicate('\n'.join(open(adb_script_path).readlines()) +
                           'exit\n')
    common.WaitProcess('adb shell script', shell_task)
    output_file.close()

  def _UnlockScreen(self):
    press_menu_and_back = ['input', 'keyevent', '82', '&&', 'input', 'keyevent',
                           '4']
    # Technically we could go and call WindowManagerService.dismissKeyguard()
    # for a large range of API levels. But there are plans to ship emulator
    # images with keyguard 100% stripped, and I would rather go down that route.
    # https://code.google.com/p/android/issues/detail?id=196287
    if self.GetApiVersion() >= 23:
      self.ExecOnDevice(press_menu_and_back + ['&&', 'wm', 'dismiss-keyguard'])
    else:
      self.ExecOnDevice(press_menu_and_back)

  def _RestartAndroid(self):
    """Restarts the user-space system."""
    if self.GetApiVersion() >= 23:
      # stop fingerprintd first, because it will block servicemanager
      self.ExecOnDevice(['stop fingerprintd'])

    if self.GetApiVersion() >= 19:
      self.ExecOnDevice(['am', 'restart'])
    else:
      self.ExecOnDevice(['stop'])
      time.sleep(1)
      self.ExecOnDevice(['start'])

    if self.GetApiVersion() >= 23:
      self.ExecOnDevice(['start fingerprintd'])

  def _CheckDpi(self):
    """Checks if DPI is set correctly."""
    logging.info('checking dpi...')
    output = self.ExecOnDevice([
        'am',
        'instrument',
        '-w',
        _BOOTSTRAP_PKG + '/.DpiCheck'])

    logging.info('dpi out: %s', output)
    dpi_ok = 'INSTRUMENTATION_CODE: -1' in output
    logging.info('dpi settings ok? %s', dpi_ok)
    if not dpi_ok:
      if self.GetApiVersion() > 10:
        return False
      else:
        logging.error('Dpi wrong - but cannot do system restarts before ICS')
        return True
    else:
      return True

  def EnableLogcat(self):
    """Enable logcat on device."""
    if not self._logcat_filter:
      return
    if not self._logcat_path:
      log_dir = os.environ.get('TEST_UNDECLARED_OUTPUTS_DIR')
      if not log_dir:
        return
      self._logcat_path = os.path.join(log_dir, 'logcat-device.txt')

    assert self._CanConnect(), 'missing details to connect to adb.'

    attemps = 0
    logcat_worked = False
    # Wait for logcat can work.
    while attemps < 30:
      attemps += 1
      output = self.ExecOnDevice(['logcat', '-d'])
      if output.startswith('logcat read failure'):
        time.sleep(1)
        continue
      logcat_worked = True
      logging.info('Logcat worked after %d attemps', attemps)
      break

    assert logcat_worked, 'Logcat can not work'

    logcat_args = [self.android_platform.adb,
                   '-s', self.device_serial]
    logcat_args.extend(['logcat', '-v', 'threadtime', '-b', 'events', '-b',
                        'main'])
    if self.GetApiVersion() >= 19:
      logcat_args.extend(['-b', 'system'])
    if self.GetApiVersion() >= 21:
      logcat_args.extend(['-b', 'crash'])
    logcat_args.append(self._logcat_filter)

    common.Spawn(
        logcat_args,
        exec_env=self._AdbEnv(),
        proc_output=open('/dev/null', 'w'),
        proc_input=_DEV_NULL,
        logfile=self._logcat_path)

  def _GetCertName(self, cert_path, cert_format=X509.FORMAT_PEM):
    cert = X509.load_cert(cert_path, format=cert_format)
    cert_name = cert.get_subject().as_hash()
    cert_name = hex(int(cert_name))[2:]
    return '%s.0' % cert_name

  def AddCert(self, cert_path, cert_format=X509.FORMAT_PEM):
    """Adds a CACert to the device.

    This is only avaliable for ICS or better API levels. Before that
    CACerts were all stored in a singular BKS keystore - making updates painful.

    Arguments:
      cert_path: a path to an X509 cert.
      cert_format: A valid cert format from X509 (defaults to PEM - ascii).
    """
    is_ics_or_better = self.GetApiVersion() > 13
    assert is_ics_or_better, 'Dynamic Certs only supported after ICS'
    cert_name = self._GetCertName(cert_path, cert_format)
    if self.GetApiVersion() < 21:
      base_dest = '/data/misc/keychain/'
    else:
      base_dest = '/data/misc/user/0/'
    dest = os.path.join(base_dest, 'cacerts-added/%s' % cert_name)
    self._Push(cert_path, dest)

  def _CanConnect(self):
    if not self.device_serial:
      self.device_serial = 'localhost:%s' % self.emulator_adb_port
    return (self.emulator_adb_port and
            self.emulator_telnet_port)

  def _LogFileContent(self, tag, f):
    logging.info('%s below ' + '=' * 50 + '\n%s', tag, f.read())
    logging.info('%s end ' + '=' * 50, tag)

  def _ShowEmulatorLog(self):
    """Show contents of various log file to help debug."""
    with self._EmulatorLogFile('r') as f:
      self._LogFileContent('Emulator log', f)

    for log in ('watchdog.out', 'watchdog.err'):
      name = os.path.join(self._emulator_tmp_dir, 'watchdog', log)
      if os.path.exists(name):
        with open(name) as f:
          self._LogFileContent(log, f)

  def _EnsureEmuRunning(self):
    assert self._emu_process_pid, 'No emu process pid!'
    wait_result, _ = os.waitpid(self._emu_process_pid, os.WNOHANG)

    if wait_result != 0:
      self._ShowEmulatorLog()
      raise Exception('Emulator has died')


class Attempter(object):
  """Tracks progress of launching the emulator."""

  def __init__(self, timer, sleep_interval):
    self._stopwatch = timer
    self.sleep_interval = sleep_interval
    self.total_attempts = 0
    self.step_attempts = 0

  def AttemptStep(self, step_fn, details, check_tag, sleep_tag):
    """Attempts to execute a particular step in launching the emulator.

    Args:
      step_fn: The function which performs the step - must return success.
      details: a message to log out before performing the step
      check_tag: the tag to pass to stopwatch to charge the exec time against
      sleep_tag: the tag to pass to stopwatch to charge sleeping time against

    Returns:
      The result of step_fn(). If result is truthy step_attempts is 0'd out
    """
    self.total_attempts += 1
    self.step_attempts += 1
    logging.info(details)
    self._stopwatch.start(check_tag)
    step_completes = step_fn()
    self._stopwatch.stop(check_tag)
    if not step_completes:
      self._stopwatch.start(sleep_tag)
      time.sleep(self.sleep_interval)
      self._stopwatch.stop(sleep_tag)
    else:
      self.step_attempts = 0
    return step_completes


class TransientEmulatorFailure(Exception):
  """Indicates the emulator could not be started or shutdown.

  These failures are transient and a subsequent launch using the same
  configuration is extremely likely to succeed.

  All state associated with this emulator should be wiped out before
  this exception is thrown.
  """
  pass


class Display(object):
  """Display options for an emulator."""

  def __init__(self,
               skin,
               tmp_dir,
               enable_display=True,
               start_vnc_on_port=0,
               open_gl_driver=HOST_OPEN_GL,
               env=None):
    self.skin = skin
    self.tmp_dir = tmp_dir
    self.start_vnc_on_port = start_vnc_on_port
    self.open_gl_driver = open_gl_driver
    self._env = env or os.environ

    if self.open_gl_driver == HOST_OPEN_GL:
      # HOST_OPEN_GL is a "dominant" option. If requested, we either provide it
      # or raise. We also ignore any any conflicting options.
      assert 'DISPLAY' in self._env, (
          'Host GPU OpenGL mode requires external $DISPLAY')
      self.x = xserver.External(env=self._env)
      self.vnc = None
    elif 'DISPLAY' in self._env and enable_display and not start_vnc_on_port:
      # Render into external X $DISPLAY unless user asked not to enable_display
      # or to record video.
      self.x = xserver.External(env=self._env)
      self.vnc = None
    else:
      # Rendering into Xvfb if explicitly asked to or if $DISPLAY isn't set.
      self.x = self._MakeX11Server()

  def Start(self):
    self.x.Start()

  @property
  def environment(self):
    """Returns a dict with env values for process to use this display."""
    return self.x.environment

  def Kill(self):
    return self.x.Kill()

  def _MakeX11Server(self):
    height = self.skin[self.skin.index('x') + 1:]
    width = self.skin[:self.skin.index('x')]
    x11_tmp_dir = os.path.join(self.tmp_dir, 'x11')
    os.makedirs(x11_tmp_dir)

    return xserver.X11Server(
        self._env['TEST_SRCDIR'],
        x11_tmp_dir,
        width,
        height)


class IdleStatus(object):
  """Show idle status of an emulator."""

  def __init__(self, device):
    self.device = device
    self.cpu_count = self._CountCpu()
    self.loads = []
    self.RecentMaxLoad()

  def RecentMaxLoad(self, minimal_time=15):
    """Return the maximum cpu load for the recent minimal_time."""

    out = self.device.ExecOnDevice(['cat', '/proc/uptime'])
    up_time, idle_time = out.split()
    up_time = float(up_time)
    idle_time = float(idle_time)
    self.loads.append(LoadInfo(timestamp=time.time(), up_time=up_time,
                               idle_time=idle_time))

    max_load = 0.0
    last_up_time = self.loads[-1].up_time
    last_idle_time = self.loads[-1].idle_time
    for load in reversed(self.loads[:-1]):
      real_time = self.loads[-1].timestamp - load.timestamp
      idle = (last_idle_time - load.idle_time)/(last_up_time - load.up_time)
      idle /= self.cpu_count
      max_load = max(max_load, 1.0 - idle)
      if real_time >= minimal_time:
        return max_load
      last_up_time = load.up_time
      last_idle_time = load.idle_time

    logging.warning('Lack of idle data, pretend to be busy.')
    return 1.0

  def _CountCpu(self):
    # TODO: fix this when we upgrading emulator to qemu2.
    return 1
