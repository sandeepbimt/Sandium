// Copyright (c) 2017 The Brave Authors. All rights reserved.
// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this file,
// You can obtain one at https://mozilla.org/MPL/2.0/.

import { existsSync, readFileSync, statSync, writeSync } from 'node:fs'
import { join } from 'node:path'
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

function validateSandiumPreparedGraph(mode: string) {
  const outputDir = config.outputDir
  const requiredFiles = [
    ['build.ninja', join(outputDir, 'build.ninja')],
    ['build.ninja.stamp', join(outputDir, 'build.ninja.stamp')],
    ['args.gn', join(outputDir, 'args.gn')],
    ['args_generated.gni', join(outputDir, 'args_generated.gni')],
  ] as const

  for (const [label, filename] of requiredFiles) {
    if (!existsSync(filename) || !statSync(filename).isFile() || statSync(filename).size === 0) {
      throw new Error(`SANDIUM ${mode} requires an existing, nonempty ${label}: ${filename}`)
    }
  }

  const argsGn = readFileSync(join(outputDir, 'args.gn'), 'utf8')
  const generatedArgs = readFileSync(join(outputDir, 'args_generated.gni'), 'utf8')
  if (!argsGn.includes('import("//out/android_Debug_arm64/args_generated.gni")')) {
    throw new Error('SANDIUM prepared graph has no expected args_generated.gni import')
  }
  for (const [name, value] of [
    ['use_siso', 'true'],
    ['use_reclient', 'false'],
    ['use_remoteexec', 'true'],
  ] as const) {
    const pattern = new RegExp(`^\\s*${name}\\s*=\\s*${value}\\s*// Copyright (c) 2017 The Brave Authors. All rights reserved.
// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this file,
// You can obtain one at https://mozilla.org/MPL/2.0/.

import { existsSync, readFileSync, statSync, writeSync } from 'node:fs'
import { join } from 'node:path'
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

, 'm')
    if (!pattern.test(generatedArgs)) {
      throw new Error(`SANDIUM prepared graph is missing effective GN argument ${name}=${value}`)
    }
  }
  writePhaseDiagnostic(1, `SANDIUM_PREPARED_GRAPH_VALIDATION=PASS mode=${mode}`)
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

  const prepareRestoredGraph =
    process.env.SANDIUM_CI_PREPARE_RESTORED_GRAPH === 'true'
  const resumePreparedGraph =
    process.env.SANDIUM_CI_RESUME_PREPARED_GRAPH === 'true'
  if (prepareRestoredGraph && resumePreparedGraph) {
    throw new Error(
      'SANDIUM_CI_PREPARE_RESTORED_GRAPH and SANDIUM_CI_RESUME_PREPARED_GRAPH cannot both be true',
    )
  }

  if (prepareRestoredGraph) {
    // A validated checkpoint already contains the GN graph. Reapply Brave's
    // source overrides/branding on this fresh stage machine, but do not run GN.
    runBuildPhase('touch-overridden-chromium-files', () =>
      util.touchOverriddenFiles(),
    )
    runBuildPhase('update-branding', () => branding.update())
    runBuildPhase('ensure-toolchain-mount', () => buildUtils.ensureVsFilesMount())
    runBuildPhase('validate-restored-ninja-graph', () =>
      validateSandiumPreparedGraph('restored checkpoint'),
    )
    writePhaseDiagnostic(1, 'SANDIUM_PREPARE_RESTORED_GRAPH=PASS')
    return
  }

  if (resumePreparedGraph) {
    // The graph was freshly prepared or restored and validated in a separate
    // CI step. Do not touch inputs or regenerate GN between that gate and Siso.
    runBuildPhase('validate-prepared-ninja-graph', () =>
      validateSandiumPreparedGraph('compile resume'),
    )
    await runBuildPhaseAsync('build-targets-siso-autoninja-resume', () =>
      util.buildTargets(),
    )
    writePhaseDiagnostic(1, 'SANDIUM_RESUME_PREPARED_GRAPH=PASS')
    return
  }

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
