// Copyright (c) 2017 The Brave Authors. All rights reserved.
// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this file,
// You can obtain one at https://mozilla.org/MPL/2.0/.

import { writeSync } from 'node:fs'
import config from './config.ts'
import util from './util.ts'
import branding from './branding.js'
import type * as buildOptions from './buildOptions.ts'
import * as buildUtils from './buildUtils.ts'

// Phase markers use synchronous writes because Commander and legacy helpers
// may terminate the process immediately after an action fails. console.log()
// can leave recent markers buffered, making the wrong internal phase appear to
// have failed in CI.
function writePhaseDiagnostic(fd: 1 | 2, message: string) {
  try {
    writeSync(fd, message + '\n')
  } catch {
    // Do not replace the original build failure with a diagnostics-write error.
  }
}

function describePhaseError(error: unknown): string {
  if (error instanceof Error) {
    return error.stack || `${error.name}: ${error.message}`
  }
  try {
    return String(error)
  } catch {
    return '<unprintable thrown value>'
  }
}

function runBuildPhase<T>(name: string, action: () => T): T {
  writePhaseDiagnostic(1, `SANDIUM_BUILD_PHASE_BEGIN=${name}`)
  try {
    const result = action()
    writePhaseDiagnostic(1, `SANDIUM_BUILD_PHASE_END=${name}`)
    return result
  } catch (error) {
    writePhaseDiagnostic(2, `SANDIUM_BUILD_PHASE_FAILED=${name}`)
    writePhaseDiagnostic(2, `SANDIUM_BUILD_PHASE_ERROR_TYPE=${typeof error}`)
    writePhaseDiagnostic(2, describePhaseError(error))
    if (!(error instanceof Error)) {
      writePhaseDiagnostic(
        2,
        describePhaseError(new Error(`Stack while handling phase ${name}`)),
      )
    }
    throw error
  }
}

async function runBuildPhaseAsync<T>(
  name: string,
  action: () => Promise<T>,
): Promise<T> {
  writePhaseDiagnostic(1, `SANDIUM_BUILD_PHASE_BEGIN=${name}`)
  try {
    const result = await action()
    writePhaseDiagnostic(1, `SANDIUM_BUILD_PHASE_END=${name}`)
    return result
  } catch (error) {
    writePhaseDiagnostic(2, `SANDIUM_BUILD_PHASE_FAILED=${name}`)
    writePhaseDiagnostic(2, `SANDIUM_BUILD_PHASE_ERROR_TYPE=${typeof error}`)
    writePhaseDiagnostic(2, describePhaseError(error))
    if (!(error instanceof Error)) {
      writePhaseDiagnostic(
        2,
        describePhaseError(new Error(`Stack while handling phase ${name}`)),
      )
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
    // SANDIUM CI uses prepare_only as a pre-compilation gate. Generate GN
    // files here so --gn values are materialized in args.gn and can be
    // validated before any expensive compile window starts.
    await runBuildPhaseAsync('generate-ninja-files-prepare-only', () =>
      util.generateNinjaFiles(),
    )
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
