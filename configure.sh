#!/usr/bin/env bash

#
# configure.sh - Setup your OpenBazaar development environment in one step.
#
# If you are a Ubuntu or MacOSX user, and you've already checked out
# the OpenBazaar sourcecode from the git repository; you can try
# configuring/installing OpenBazaar by simply executing this script
# instead of following the build instructions in the OpenBazaar Wiki
# https://github.com/OpenBazaar/OpenBazaar/wiki/Build-Instructions
#
# This script will only get better as its tested on more development environments.
# If you can't modify it to make it better, please open an issue with a full
# error report at https://github.com/OpenBazaar/OpenBazaar/issues/new
#
#

UBUNTU_PACKAGES="python-pip build-essential rng-tools python-dev libjpeg-dev sqlite3 openssl alien libssl-dev python-virtualenv lintian libjs-jquery"
ARCH_PACKAGES="python2 python2-pip python2-virtualenv rng-tools libjpeg sqlite3 openssl"
RASPIARCH_PACKAGES="base-devel curl wget python2 python2-pip rng-tools libjpeg sqlite3 openssl libunistring"
RASPBIAN_PACKAGES="python-pip build-essential rng-tools alien openssl libssl-dev python-dev libjpeg-dev sqlite3 haveged"
PORTAGE_PACKAGES="dev-lang/python:2.7 dev-python/pip rng-tools gcc jpeg sqlite3 openssl dev-python/virtualenv"
FEDORA_PACKAGES="kernel-devel rng-tools openssl openssl-libs openssl-devel openjpeg openjpeg-devel make alien python2 python-pip python-virtualenv python-devel python-zmq zeromq3 zeromq3-devel pyOpenSSL"
SLACK_PACKAGES="python pysetuptools pip virtualenv rng-tools libjpeg sqlite openssl" # if you change this, check the installSlack function carefully



# exit on error
set -e

function command_exists {
  # POSIX-compatible way to check if a command exists.
  # usage: "if command_exists foo; then echo it exists; fi"
  command -v "$1" > /dev/null
}

function make_env {
  # Create a virtualenv for OpenBazaar.
  # NOTE: we get env/bin/pip by doing this.
  if [ ! -d "./env" ]; then
    virtualenv --python=python2.7 env
  fi
}

function brewDoctor {
    if ! brew doctor; then
      echo
      echo "'brew doctor' did not exit cleanly! This may be okay. Read above."
      echo
      read -p "Press [Enter] to continue anyway or [ctrl + c] to exit and do what the doctor says..."
    fi
}

function brewUpgrade {
    echo "If your homebrew packages are outdated, we recommend upgrading them now. This may take some time."
    echo "We will only upgrade the packages needed to run OpenBazaar"
    read -r -p "Do you want to do this? [y/N] " response
    if [[ $response =~ ^([yY][eE][sS]|[yY])$ ]]
    then
      if ! brew upgrade autoenv gpg openssl sqlite3 wget; then
        echo
        echo "There were errors when attempting 'brew upgrade', there could be issues with the installation of OpenBazaar."
        echo
        read -p "Press [Enter] to continue anyway or [ctrl + c] to exit and fix those errors."
      fi
    fi
}

function installMac {
  ORIGINAL_CPPFLAGS=$CPPFLAGS
  ORIGINAL_CXXFLAGS=$CXXFLAGS
  ORIGINAL_DYLD_LIBRARY_PATH=$DYLD_LIBRARY_PATH
  unset CPPFLAGS
  unset CXXFLAGS
  unset DYLD_LIBRARY_PATH

  # Install brew if it is not installed, otherwise upgrade it.
  if ! command_exists brew ; then
    echo "Installing brew..."
    ruby -e "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/master/install)"
  else
    echo "Updating, Upgrading and checking brew installation..."
    brew update
    brewDoctor
    brewUpgrade
    brew prune
  fi

  # Use brew's python 2.7, even if user has a system python.
  # The brew version comes with pip and setuptools.
  # If user already has brew installed python, then this won't do anything.
  # Note we get pip for free by doing this, and can avoid future calls to sudo.
  # brew convention abhors all things 'sudo' anyway.
  brew install python

  for dep in gpg sqlite3 wget openssl autoenv
  do
    if ! command_exists $dep ; then
      brew install $dep
    fi
  done

  # Install python's virtualenv if it is not installed.
  if ! command_exists virtualenv ; then
    pip install virtualenv
  fi

  make_env

  # "To begin using the virtual environment, it needs to be activated:"
  # http://docs.python-guide.org/en/latest/dev/virtualenvs/
  # We have autoenv and an appropriate .env in our OB home dir,
  # but we should activate the env just in case
  # (e.g. for first time or non-Mac users).
  source env/bin/activate

  # Set compile flags for brew's openssl instead of using brew link --force
  export CFLAGS="-I$(brew --prefix openssl)/include"
  export LDFLAGS="-L$(brew --prefix openssl)/lib"

  # Install python deps inside our virtualenv
  ./env/bin/pip install -r requirements.txt

  # restore any previous build flags
  export CPPFLAGS=$ORIGINAL_CPPFLAGS
  export CXXFLAGS=$ORIGINAL_CXXFLAGS
  export DYLD_LIBRARY_PATH=$ORIGINAL_DYLD_LIBRARY_PATH
}

function sudoMessage {
  echo
  echo "In order to install OpenBazaar, this script will install the following requirements in your system:"
  echo "  $*"
  echo "Note: this script requires sudo for the installation."
  echo
}

function doneMessage {
  VERSION_FROM_CHANGELOG="$(head -1 changelog | awk '/openbazaar \(.*\..*\..*\)/ { print $2 }')"
  echo
  echo
  echo
  echo '   ____                     ____                            '
  echo '  / __ \                   |  _ \                           '
  echo ' | |  | |_ __   ___ _ __   | |_) | __ _ ______ _  __ _ _ __ '
  echo ' | |  | |  _ \ / _ \  _ \  |  _ < / _` |_  / _` |/ _` |  __|'
  echo ' | |__| | |_) |  __/ | | | | |_) | (_| |/ / (_| | (_| | |   '
  echo '  \____/| .__/ \___|_| |_| |____/ \__,_/___\__,_|\__,_|_|   '
  echo '        | |                                                 '
  echo '        |_|                                                 '                                                                                                   ##'
  echo
  echo "                                             Release $VERSION_FROM_CHANGELOG"
  echo
  echo
  echo "OpenBazaar configuration finished!"
  echo "Run './openbazaar $1start; tail -F logs/production.log' to start OpenBazaar and output logs."
  echo
  echo
  echo
}

function confirm {
    # call with a prompt string or use a default Y
    read -r -p "Are you sure? [Y/n] " response
    if [[ $response =~ ^([yY][eE][sS]|[yY])$ ]]; then
      return 0
    else
      return 1
    fi
}

function installUbuntu {
  sudoMessage ${UBUNTU_PACKAGES}

  sudo apt-get --quiet update || echo 'apt-get update failed. Continuing...'
  sudo apt-get --assume-yes install ${UBUNTU_PACKAGES}

  make_env

  ./env/bin/pip install -r requirements.txt
}

function installArch {
  sudoMessage ${ARCH_PACKAGES}

  echo "Some packages and dependencies may fail to install if your package list is out of date."
  echo "Would you like to upgrade your system now? "
  if confirm ; then
    sudo pacman --sync --refresh --sysupgrade
  else
    echo "Continuing."
  fi
  # sudo pacman --sync --needed base-devel
  # Can conflict with multilib packages. Uncomment previous line if you don't already have base-devel installed
  sudo pacman --sync --needed ${ARCH_PACKAGES}

  make_env

  ./env/bin/pip install -r requirements.txt
}

function installRaspiArch {
  sudoMessage ${RASPIARCH_PACKAGES}

  # pacman --sync sudo
  sudo pacman --sync --refresh
  sudo pacman --sync --needed ${RASPIARCH_PACKAGES}
  echo " "
  echo "Notice : pip install requires 10~30 minutes to complete."
  if confirm ; then
    pip2 install -r requirements.txt
    echo "Run OpenBazaar on Raspberry Pi Arch without HDMI/VideoOut"
    echo "Type the following shell command to start."
    echo " "
    echo "IP=\$(/sbin/ifconfig eth0 | grep 'inet ' | awk '{print \$2}')"
    echo "./openbazaar --disable-open-browser -k \$IP -q 8888 -p 12345 start; tail -F logs/production.log"
  fi
}

function installRaspbian {
  sudoMessage ${RASPBIAN_PACKAGES}

  sudo apt-get --assume-yes install ${RASPBIAN_PACKAGES}
  echo " "
  echo "Notice : pip install requires 2~3 hours to complete."
  if confirm ; then
    sudo pip install --requirement requirements.txt
    echo "Run OpenBazaar on Raspberry Pi Raspbian without HDMI/VideoOut"
    echo "Type the following shell command to start."
    echo " "
    echo "IP=\$(/sbin/ifconfig eth0 | grep 'inet addr:' | cut -d: -f2 | awk '{ print \$1}')"
    echo "./openbazaar --disable-open-browser -k \$IP -q 8888 -p 12345 start; tail -F logs/production.log"
  fi
}

function installPortage {
  sudoMessage ${PORTAGE_PACKAGES}

  sudo emerge --noreplace ${PORTAGE_PACKAGES}

  make_env

  ./env/bin/pip install -r requirements.txt
}

function installFedora {
  sudoMessage ${FEDORA_PACKAGES} bitcoin-release openssl-compat-bitcoin-libs

  sudo yum --assumeyes install ${FEDORA_PACKAGES}
  rpm --query bitcoin-release || sudo yum --assumeyes install http://linux.ringingliberty.com/bitcoin/f20/x86_64/bitcoin-release-1-6.noarch.rpm
  sudo yum --assumeyes install openssl-compat-bitcoin-libs

  make_env

  ./env/bin/pip install -r requirements.txt
}

function installSlack {
  sudoMessage ${SLACK_PACKAGES}

  sudo /usr/sbin/slackpkg update
  if ! command_exists python; then
    sudo /usr/sbin/slackpkg install python
  fi

  if [ ! -f /usr/sbin/sbopkg ];  then
      echo "Please install sbopkg for ease of dependency installation from sbopkgs. "
           "Be sure to run sbopkg and sync before retrying this install."
      exit 1
  else
      if ! command_exists pip; then
        sudo /usr/sbin/sbopkg -i pysetuptools # Required for pip
        sudo /usr/sbin/sbopkg -i pip
      fi

      sudo pip install virtualenv
      wget http://sourceforge.net/projects/gkernel/files/rng-tools/5/rng-tools-5.tar.gz
      tar -xvf rng-tools-5.tar.gz
      pushd rng-tools-5
      ./configure
      make
      sudo make install
      popd
      sudo /usr/sbin/slackpkg install libjpeg sqlite openssl
   fi

   make_env

  ./env/bin/pip install -r requirements.txt
}

echo "Detecting OS..."
if [[ $OSTYPE == darwin* ]] ; then
  echo "Found OS X"
  installMac
  # There are still pysqlcipher issues on OS X.
  # Suggest disabling sqlite-crypt until that is resolved.
  doneMessage "--disable-sqlite-crypt "
elif [[ $OSTYPE == linux-gnu || $OSTYPE == linux-gnueabihf ]]; then
  UNAME=$(uname -a)
  echo "Found Linux (${UNAME})"
  if [ -f /etc/arch-release ]; then
      if [[ "$UNAME" =~ alarmpi ]]; then
          echo "Found distribution Raspberry Pi Arch Linux"
          installRaspiArch "$@"
      else
          echo "Found distribution Arch Linux"
          installArch
      fi
  elif [ -f /etc/manjaro-release ]; then
    echo "Found distribution Arch Linux"
    installArch
  elif [ -f /etc/gentoo-release ]; then
    echo "Found distribution Gentoo"
    installPortage
  elif [ -f /etc/fedora-release ]; then
    echo "Found distribution Fedora"
    installFedora
  elif [ -f /etc/slackware-version ]; then
    echo "Found distribution Slack"
    installSlack
  elif grep Raspbian /etc/os-release ; then
    echo "Found distribution Raspberry Pi Raspbian"
    installRaspbian "$@"
  else
    echo "Found distribution Ubuntu or other"
    installUbuntu
  fi
  doneMessage
fi
