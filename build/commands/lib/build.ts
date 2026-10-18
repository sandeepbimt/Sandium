// Copyright (c) 2017 The Brave Authors. All rights reserved.
// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this file,
// You can obtain one at https://mozilla.org/MPL/2.0/.

import config from './config.ts'
import util from './util.ts'
import branding from './branding.js'
import type * as buildOptions from './buildOptions.ts'
import * as buildUtils from './buildUtils.ts'

function runBuildPhase<T>(name: string, action: () => T): T {
  console.log(`SANDIUM_BUILD_PHASE_BEGIN=${name}`)
  try {
    const result = action()
    console.log(`SANDIUM_BUILD_PHASE_END=${name}`)
    return result
  } catch (error) {
    console.error(`SANDIUM_BUILD_PHASE_FAILED=${name}`)
    console.error(`SANDIUM_BUILD_PHASE_ERROR_TYPE=${typeof error}`)
    console.error(
      error instanceof Error
        ? error.stack || error.message
        : String(error),
    )
    if (!(error instanceof Error)) {
      console.error(new Error(`Stack while handling phase ${name}`).stack)
    }
    throw error
  }
}

async function runBuildPhaseAsync<T>(
  name: string,
  action: () => Promise<T>,
): Promise<T> {
  console.log(`SANDIUM_BUILD_PHASE_BEGIN=${name}`)
  try {
    const result = await action()
    console.log(`SANDIUM_BUILD_PHASE_END=${name}`)
    return result
  } catch (error) {
    console.error(`SANDIUM_BUILD_PHASE_FAILED=${name}`)
    console.error(`SANDIUM_BUILD_PHASE_ERROR_TYPE=${typeof error}`)
    console.error(
      error instanceof Error
        ? error.stack || error.message
        : String(error),
    )
    if (!(error instanceof Error)) {
      console.error(new Error(`Stack while handling phase ${name}`).stack)
    }
    throw error
  }
}

export async function build(
  buildConfig = config.defaultBuildConfig,
  options: buildOptions.BuildDirOptions
    & buildOptions.TargetConfigOptions
    & buildOptions.GnArgsOptions
    & buildOptions.GnGenOptions
    & buildOptions.NinjaOptions,
) {
  config.buildConfig = buildConfig
  runBuildPhase('config-update', () => config.update(options))
  runBuildPhase('check-versions', () => buildUtils.checkVersionsMatch())

  runBuildPhase('touch-overridden-chromium-files', () =>
    util.touchOverriddenFiles(),
  )
  runBuildPhase('update-branding', () => branding.update())
  runBuildPhase('ensure-toolchain-mount', () => buildUtils.ensureVsFilesMount())

  if (options.prepare_only) {
    return
  }

  if (config.xcode_gen_target) {
    runBuildPhase('generate-xcode-workspace', () => util.generateXcodeWorkspace())
  } else {
    if (!config.use_no_gn_gen) {
      await runBuildPhaseAsync('generate-ninja-files', () =>
        util.generateNinjaFiles(),
      )
    }
    await runBuildPhaseAsync('build-targets-siso-autoninja', () =>
      util.buildTargets(),
    )
  }
}
