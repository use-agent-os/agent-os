export const setup = {
  'setup.brand': 'AgentOS',
  'setup.brand.platform': 'for Mac',
  'setup.step.install': 'Install',
  'setup.step.provider': 'Provider',
  'setup.step.ready': 'Ready',

  'setup.install.title': 'Welcome to AgentOS',
  'setup.install.blurb':
    'Your agent runs on this Mac. The engine installs in the background in a few minutes; you only do this once.',
  'setup.update.title': 'A newer engine is ready',
  'setup.update.blurb':
    'This app ships a newer engine than the one on this Mac. It updates in place and keeps your sessions and settings.',
  'setup.found': 'Installed',
  'setup.ships': 'This app installs',
  'setup.install': 'Install the engine',
  'setup.update': 'Update the engine',
  'setup.connectExisting': 'I already run a gateway',
  'setup.connectExisting.help': 'Connect to one on this Mac or elsewhere instead of installing.',

  'setup.running.install': 'Installing the engine',
  'setup.running.update': 'Updating the engine',
  'setup.running.blurb': 'Downloading and installing. Leave this window open.',
  'setup.steps': 'steps complete',
  'setup.details.show': 'Show details',
  'setup.details.hide': 'Hide details',
  'setup.output': 'Installer output',
  'setup.lines': 'lines',
  'setup.cancel': 'Cancel',
  'setup.cancelling': 'Stopping…',

  'setup.failed.title': 'Setup did not finish',
  'setup.cancelled.title': 'Setup was cancelled',
  'setup.failed.blurb':
    'Nothing else on this Mac was changed. Retry, or install from a terminal with the command below and reopen the app.',
  'setup.retry': 'Retry',
  'setup.copyOutput': 'Copy output',
  'setup.copied': 'Output copied',
  'setup.openLog': 'Show log in Finder',
  'setup.manual': 'Install from a terminal',

  'setup.done.title': 'Engine installed',
  'setup.done.starting': 'Starting the gateway…',
  'setup.done.waiting': 'The gateway is taking a while to come up.',
  'setup.done.continue': 'Continue',

  // Provider step
  'setup.provider.title': 'Who answers your messages?',
  'setup.provider.blurb':
    'Pick a provider and paste its API key. You can add or change providers any time in Settings.',
  'setup.provider.later': 'Skip for now',
  'setup.provider.back': 'All providers',
  'setup.provider.saving': 'Saving and restarting the gateway…',
  'setup.provider.saved': 'Provider saved.',
  'setup.provider.savedRestart': 'Provider saved. Restart the gateway to apply.',
  'setup.provider.loading': 'Loading providers…',

  'setup.ready.title': "You're all set",
  'setup.ready.blurb': 'is answering your messages. Ask anything to get started.',
  'setup.ready.blurb.none':
    'No provider yet. Pick one in Settings › Providers before your first message.',
  'setup.ready.open': 'Start chatting',
  'setup.ready.checking': 'Checking the key with the provider…',
  'setup.ready.keyOk': 'Key verified.',
  'setup.ready.models': 'models available',
  'setup.ready.keyBad': 'The provider did not accept this key:',
  'setup.ready.keyUnknown': 'Saved, but the key could not be verified right now.',
  'setup.ready.editKey': 'Edit key',
  'setup.ready.continueAnyway': 'Continue anyway',

  // Settings › Advanced
  'settings.advanced.engine': 'Engine',
  'settings.advanced.engine.help': 'The use-agent-os package this app installed.',
  'settings.advanced.reinstall': 'Reinstall engine',
  'settings.advanced.reinstall.help':
    'Installs the engine this app ships again, over what is there. The gateway restarts.',
  'settings.advanced.uninstall': 'Remove engine',
  'settings.advanced.uninstall.help':
    'Stops the gateway and removes the use-agent-os package. Your data in ~/.agentos stays.',
  'settings.advanced.uninstallConfirm.title': 'Remove the AgentOS engine?',
  'settings.advanced.uninstallConfirm.body':
    'The gateway stops and the use-agent-os package is uninstalled. Sessions, projects and configuration in ~/.agentos are kept; reopening the app offers to install again.',
  'settings.advanced.uninstallConfirm.confirm': 'Remove',
  'settings.advanced.uninstallDone': 'Engine removed',
  'settings.advanced.uninstallFailed': 'Could not remove the engine',
} as const
