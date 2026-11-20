/** Settings › Memory. Plain words: what the agent remembers, how it finds it again. */
export const memory = {
  'settings.memory.dash': '—',
  'settings.memory.cancel': 'Cancel',
  'settings.memory.close': 'Close',
  'settings.memory.retry': 'Try again',
  'settings.memory.advanced': 'Advanced',
  'settings.memory.saving': 'Saving…',
  'settings.memory.saved': 'Saved',
  'settings.memory.removed': 'Removed',
  'settings.memory.applied': 'Applied. No restart needed.',
  'settings.memory.unsaved': 'Unsaved changes',
  'settings.memory.live': 'Applies right away, no restart.',
  'settings.memory.conflict':
    'The settings changed elsewhere and were reloaded. Your edit is kept: check it and press Save again.',
  'settings.memory.offlineList': 'Not connected to the gateway.',
  'settings.memory.view': 'View',
  'settings.memory.viewNamed': 'View {name}',
  'settings.memory.unit.chars': 'characters',
  'settings.memory.unit.second': 'second',
  'settings.memory.unit.seconds': 'seconds',
  'settings.memory.unit.message': 'message',
  'settings.memory.unit.messages': 'messages',
  'settings.memory.n.char.one': '{n} character',
  'settings.memory.n.char.many': '{n} characters',
  'settings.memory.n.line.one': '{n} line',
  'settings.memory.n.line.many': '{n} lines',
  'settings.memory.n.passage.one': '{n} passage',
  'settings.memory.n.passage.many': '{n} passages',
  'settings.memory.n.file.one': '{n} file',
  'settings.memory.n.file.many': '{n} files',
  'settings.memory.n.fact.one': '{n} fact',
  'settings.memory.n.fact.many': '{n} facts',
  'settings.memory.n.notePassage.one': '{n} note passage',
  'settings.memory.n.notePassage.many': '{n} note passages',
  'settings.memory.n.part.one': '{n} indexed part of the conversation',
  'settings.memory.n.part.many': '{n} indexed parts of the conversation',
  'settings.memory.n.doc.one': '{n} document',
  'settings.memory.n.doc.many': '{n} documents',

  // Head and summary
  'settings.memory.pill.meaning': 'Finds by meaning',
  'settings.memory.pill.keywords': 'Keyword search only',
  'settings.memory.pill.offline': 'Offline',
  'settings.memory.pill.checking': 'Checking',
  'settings.memory.pill.unavailable': 'Search unavailable',
  'settings.memory.stats': 'Memory at a glance',
  'settings.memory.stat.facts': 'Remembered facts',
  'settings.memory.stat.facts.hint': '{work} on work · {you} about you',
  'settings.memory.stat.docs': 'Documents',
  'settings.memory.stat.docs.hint': 'In the knowledge base',
  'settings.memory.stat.chunks': 'Indexed passages',
  'settings.memory.stat.chunks.hint.one': 'From 1 file',
  'settings.memory.stat.chunks.hint': 'From {files} files',
  'settings.memory.stat.search': 'Search',
  'settings.memory.stat.search.meaning': 'By meaning',
  'settings.memory.stat.search.keywords': 'Keywords only',
  'settings.memory.tabs': 'Memory',
  'settings.memory.tab.remembered': 'Remembered',
  'settings.memory.tab.knowledge': 'Knowledge',
  'settings.memory.tab.try': 'Try it',
  'settings.memory.tab.setup': 'Setup',

  // Remembered
  'settings.memory.work.title': 'About your work',
  'settings.memory.work.blurb': 'Projects, people, places, routines, and the agent’s own notes.',
  'settings.memory.you.title': 'About you',
  'settings.memory.you.blurb':
    'Your name, how to address you, your language, and how you like answers.',
  'settings.memory.add': 'Add',
  'settings.memory.addTo': 'Add a fact to {list}',
  'settings.memory.addFact': 'Add a fact',
  'settings.memory.edit': 'Edit',
  'settings.memory.editNamed': 'Edit “{fact}”',
  'settings.memory.remove': 'Remove',
  'settings.memory.removeNamed': 'Remove “{fact}”',
  'settings.memory.space': 'Space used',
  'settings.memory.space.caption': '{used} of {limit} characters',
  'settings.memory.nearFull':
    'Almost full. To keep saving, the agent merges or shortens entries, so check now and then that nothing still true was dropped.',
  'settings.memory.full':
    'Full: there is no room for another fact. Edit or remove one to make room, or raise the limit under Setup.',
  'settings.memory.full.raise': 'Raise the limit',
  'settings.memory.full.add': 'This list is full. Edit or remove a fact to make room.',
  'settings.memory.loadFailed':
    '{file} could not be read, so it cannot be changed here and the agent is not using it. Check the file’s permissions; nothing in it was deleted.',
  'settings.memory.curatedFailed': 'Could not load what the agent remembers.',
  'settings.memory.empty.title': 'Nothing remembered yet',
  'settings.memory.empty.work':
    'The agent fills this in as you chat: projects, people and routines worth keeping. You can add a fact yourself too.',
  'settings.memory.empty.you':
    'The agent learns this as you chat. You can tell it now: your name, your language, how you like answers.',
  'settings.memory.filter': 'Filter facts',
  'settings.memory.filter.none': 'No fact matches “{query}”.',
  'settings.memory.editor.label': 'Fact',
  'settings.memory.editor.placeholder.work':
    'e.g. The quarterly report is due on the first Monday of each quarter.',
  'settings.memory.editor.placeholder.you': 'e.g. Prefers short answers, in English.',
  'settings.memory.editor.hint': '⌘↩ to save, Esc to cancel',
  'settings.memory.editor.room': 'Room for {room} in this list. ⌘↩ to save, Esc to cancel',
  'settings.memory.editor.left': '{left} left in this list',
  'settings.memory.editor.over': 'Too long by {over}. This list has room for {room}.',
  'settings.memory.editor.over.break':
    'Too long by {over}. This list has room for {room}; the break before a new fact takes 3.',
  'settings.memory.editor.bare':
    'A fact cannot be only “§”: in the file, that mark is what separates facts. Write the fact in words.',
  'settings.memory.editor.split':
    'A line with only “§” would split this into two facts. Remove that line or add words to it.',
  'settings.memory.editor.gone':
    'This fact changed since you started editing, and the list has been refreshed. Your text is kept: save it as a new fact, or cancel.',
  'settings.memory.saveAsNew': 'Save as new fact',
  'settings.memory.fact.stale':
    'This fact changed since the list was loaded. The list has been refreshed.',
  'settings.memory.fact.ambiguous':
    'Another fact contains this same text, so this one cannot be changed on its own here. Change the longer fact first.',
  'settings.memory.fact.full':
    'There is not enough room in this list any more. The list has been refreshed.',
  'settings.memory.fact.damaged':
    '{file} was edited outside the app and no longer reads as a list of facts, so changes are refused to keep its text safe. A backup was saved next to it.',
  'settings.memory.fact.unreadable': '{file} could not be read, so nothing was changed.',
  'settings.memory.fact.failed': 'Could not change this list: {error}',
  'settings.memory.saveFact': 'Save',
  'settings.memory.confirmRemove.title': 'Remove this fact?',
  'settings.memory.confirmRemove.confirm': 'Remove',

  // Knowledge
  'settings.memory.docs.title': 'Documents',
  'settings.memory.docs.blurb':
    'Files the agent can look things up in when a question needs them. They are searched, not added to every message.',
  'settings.memory.docs.choose': 'Choose files',
  'settings.memory.docs.drop': 'Drop text files here',
  'settings.memory.docs.dropActive': 'Drop to add',
  'settings.memory.docs.dropHint':
    'Drop more files anywhere on this card. Text, Markdown, CSV, JSON, HTML and code, up to 10 MB each.',
  'settings.memory.docs.empty.body':
    'Notes, manuals or exports the agent should be able to look things up in. Text, Markdown, CSV, JSON, HTML and code, up to 10 MB each.',
  'settings.memory.docs.uploading': 'Adding {name}…',
  'settings.memory.docs.added.one': 'Document added. The agent can search it now.',
  'settings.memory.docs.added.many': '{count} documents added. The agent can search them now.',
  'settings.memory.docs.replaced.one': 'Document replaced. The agent searches the new version now.',
  'settings.memory.docs.replaced.many':
    '{count} documents replaced. The agent searches the new versions now.',
  'settings.memory.docs.addedReplaced':
    '{added} added and {replaced} replaced. The agent can search them now.',
  'settings.memory.docs.replace.title.one': 'Replace {name}?',
  'settings.memory.docs.replace.title.many': 'Replace {count} documents?',
  'settings.memory.docs.replace.body.one':
    'The knowledge base already has a document named {name}. The new file takes its place, and the agent only finds the new version.',
  'settings.memory.docs.replace.body.many':
    'The knowledge base already has documents with these names. The new files take their place, and the agent only finds the new versions.',
  'settings.memory.docs.replace.alsoNew': 'The other {files} in this batch are new.',
  'settings.memory.docs.replace.alsoNew.one': 'The other file in this batch is new.',
  'settings.memory.docs.replace.confirm': 'Replace',
  'settings.memory.docs.replace.folder.title': 'Add to the {name} folder?',
  'settings.memory.docs.replace.folder.body':
    'The knowledge base already has a {name} folder. Files in it with the same names as the new ones are replaced.',
  'settings.memory.docs.replace.folder.confirm': 'Add and replace',
  'settings.memory.docs.failed': 'Could not add {name}: {error}',
  'settings.memory.docs.binary':
    '{name}: PDF, Word and PowerPoint files are read on the gateway. Put the file in the agent’s workspace and add it under “Add from the workspace”.',
  'settings.memory.docs.unsupported': '{name}: this kind of file cannot be searched.',
  'settings.memory.docs.tooBig': '{name} is larger than 10 MB.',
  'settings.memory.docs.nothingFound': 'Nothing to add in {path}.',
  'settings.memory.docs.meta': '{lines} · {size} · {date}',
  'settings.memory.docs.remove': 'Remove {name}',
  'settings.memory.docs.removed': 'Removed {name}',
  'settings.memory.docs.confirmRemove.title': 'Remove {name}?',
  'settings.memory.docs.confirmRemove.body':
    'The agent will no longer find anything in it. Its copy in the knowledge base is deleted.',
  'settings.memory.docs.loadFailed': 'Could not list the documents.',
  'settings.memory.docs.path.title': 'Add from the workspace',
  'settings.memory.docs.path.hint': 'PDF, Word, PowerPoint, whole folders',
  'settings.memory.docs.path.label': 'File or folder',
  'settings.memory.docs.path.help':
    'A path inside the agent’s workspace. It is copied into the knowledge base and read there.',
  'settings.memory.docs.path.add': 'Add',
  'settings.memory.docs.path.adding': 'Adding…',
  'settings.memory.sources.title': 'Other sources',
  'settings.memory.sources.blurb':
    'Notes the agent writes for itself and searches later. Read-only here.',
  'settings.memory.sources.empty':
    'No notes yet. The agent writes dated notes here when something is too long for a single fact.',
  'settings.memory.sources.failed': 'Could not list the notes.',
  'settings.memory.viewer.blurb': 'Read-only. This is the text the agent searches.',
  'settings.memory.viewer.truncated': 'Showing the start of this file.',
  'settings.memory.viewer.failed': 'Could not open this file.',
  'settings.memory.viewer.empty': 'This file is empty.',

  // Try it
  'settings.memory.try.title': 'Test what the agent would find',
  'settings.memory.try.blurb':
    'Search the way the agent does when it looks something up, and see where each match came from.',
  'settings.memory.try.label': 'Search memory',
  'settings.memory.try.placeholder':
    'Ask the way you would ask the agent, e.g. “when is the report due”',
  'settings.memory.try.source': 'Where to look',
  'settings.memory.try.source.all': 'All',
  'settings.memory.try.source.memory': 'Memory',
  'settings.memory.try.source.knowledge': 'Knowledge',
  'settings.memory.try.source.sessions': 'Sessions',
  'settings.memory.try.weak': 'Include weak matches',
  'settings.memory.try.userNote':
    'Facts in “About you” are not searched: the agent has them in mind in every chat.',
  'settings.memory.try.idle.title': 'Type to search',
  'settings.memory.try.idle.body':
    'Use the words you would use with the agent. Matches appear as you type.',
  'settings.memory.try.searching': 'Searching',
  'settings.memory.try.none.title': 'Nothing found for “{query}”',
  'settings.memory.try.none.body': 'Try other words, look in All, or include weak matches.',
  'settings.memory.try.none.keywords':
    'Search is keyword-only right now, so the words themselves have to appear.',
  'settings.memory.try.sessionsOff':
    'Past conversations are not indexed, so Sessions finds nothing. They are included when this is on:',
  'settings.memory.try.failed': 'Search failed.',
  'settings.memory.try.results.one': '1 match, best first',
  'settings.memory.try.results.many': '{count} matches, best first',
  'settings.memory.try.resultsLabel': 'Search results',
  'settings.memory.try.lines': 'lines {start}–{end}',
  'settings.memory.try.line': 'line {line}',
  'settings.memory.try.score': '{score} match',
  'settings.memory.source.memory': 'Memory',
  'settings.memory.source.knowledge_base': 'Knowledge',
  'settings.memory.source.sessions': 'Session',
  'settings.memory.recall.title': 'Recent recall',
  'settings.memory.recall.blurb': 'What was looked up and added before recent replies.',
  'settings.memory.recall.empty':
    'Nothing yet. After you chat, each reply’s lookup shows up here (kept until the gateway restarts).',
  'settings.memory.recall.off': 'Recall before each reply is off. Turn it on under Setup.',
  'settings.memory.recall.failed': 'Could not read recent recall.',
  'settings.memory.recall.status.injected': 'Added {chars}',
  'settings.memory.recall.status.none': 'Nothing relevant',
  'settings.memory.recall.status.skipped': 'Skipped',
  'settings.memory.recall.status.unavailable': 'Unavailable',
  'settings.memory.recall.status.error': 'Failed',
  'settings.memory.recall.decision.deep': 'Dug deeper',
  'settings.memory.recall.decision.quickHit': 'Quick match',
  'settings.memory.recall.decision.quick': 'Quick lookup',
  'settings.memory.recall.hits': '{hits} found',
  'settings.memory.recall.ms': '{ms} ms',
  'settings.memory.time.now': 'just now',
  'settings.memory.time.min': '{n} min ago',
  'settings.memory.time.h': '{n} h ago',
  'settings.memory.time.d': '{n} d ago',

  // Setup: how memory is searched
  'settings.memory.setup.offline': 'Setup needs the gateway. Start it to change these settings.',
  'settings.memory.embed.title': 'How memory is searched',
  'settings.memory.embed.blurb':
    'Searching by meaning finds a fact even when you word it differently. Choose what does that work.',
  'settings.memory.embed.choices': 'Search engine',
  'settings.memory.embed.auto': 'Automatic',
  'settings.memory.embed.auto.blurb': 'A built-in model on this Mac. Nothing to set up.',
  'settings.memory.embed.local': 'On this Mac only',
  'settings.memory.embed.local.blurb': 'The built-in model, never a cloud service.',
  'settings.memory.embed.ollama': 'Ollama',
  'settings.memory.embed.ollama.blurb': 'An embedding model from your Ollama app.',
  'settings.memory.embed.openai': 'OpenAI',
  'settings.memory.embed.openai.blurb': 'OpenAI’s embedding API. Memory text is sent to OpenAI.',
  'settings.memory.embed.compatible': 'Other API',
  'settings.memory.embed.compatible.blurb': 'Any OpenAI-compatible embedding service.',
  'settings.memory.embed.none': 'Keywords only',
  'settings.memory.embed.none.blurb': 'No model. Finds the exact words only.',
  'settings.memory.embed.tag.recommended': 'Recommended',
  'settings.memory.embed.tag.private': 'Private',
  'settings.memory.embed.tag.local': 'Runs locally',
  'settings.memory.embed.tag.key': 'Needs a key',
  'settings.memory.embed.model': 'Model',
  'settings.memory.embed.model.help': 'Empty uses {model}.',
  'settings.memory.embed.baseUrl': 'Address',
  'settings.memory.embed.baseUrl.ollama': 'Where Ollama listens, seen from the gateway.',
  'settings.memory.embed.baseUrl.compatible': 'The service’s base URL, usually ending in /v1.',
  'settings.memory.embed.apiKey': 'API key',
  'settings.memory.embed.apiKey.help': 'Stored in the gateway’s configuration.',
  'settings.memory.embed.apiKey.placeholder': 'Paste a key',
  'settings.memory.embed.apiKey.saved': 'Saved. Paste a new one to replace it.',
  'settings.memory.embed.showKey': 'Show key',
  'settings.memory.embed.hideKey': 'Hide key',
  'settings.memory.embed.apiKeyEnv': 'Key from an environment variable',
  'settings.memory.embed.apiKeyEnv.help':
    'The name of a variable on the gateway that holds the key. Used when no key is pasted.',
  'settings.memory.embed.onnxDir': 'Model folder',
  'settings.memory.embed.onnxDir.help':
    'A folder with your own ONNX embedding model. Empty uses the built-in one.',
  'settings.memory.embed.onnxDir.placeholder': 'Built-in model',
  'settings.memory.embed.fallback': 'Cloud fallback',
  'settings.memory.embed.fallback.on': 'On · key from {name}',
  'settings.memory.embed.fallback.onStored': 'On · with its own key',
  'settings.memory.embed.fallback.off': 'Off · only used if the model on this Mac cannot run',
  'settings.memory.embed.fallback.onBody':
    'Used only when the model on this Mac cannot run. Memory text is then sent to {url}.',
  'settings.memory.embed.fallback.offBody':
    'Off. If the model on this Mac cannot run, search finds exact words only. Nothing is sent to a cloud service.',
  'settings.memory.embed.fallback.removed':
    'The cloud fallback is removed when you save. If the model on this Mac cannot run, search then finds exact words only.',
  'settings.memory.embed.fallback.add': 'Add a cloud fallback',
  'settings.memory.embed.fallback.remove': 'Remove cloud fallback',
  'settings.memory.embed.needsKey': 'Paste an API key or name the variable that holds it.',
  'settings.memory.embed.envUnset':
    '{name} is not set on the gateway, so search would fail. Paste a key, or set {name} under Environment.',
  'settings.memory.embed.envUnset.short': 'The key’s variable is not set on the gateway.',
  'settings.memory.embed.openEnv': 'Open Environment',
  'settings.memory.embed.needsUrl': 'Enter the service’s address.',
  'settings.memory.embed.foot.restart': 'Takes effect when the gateway restarts.',
  'settings.memory.embed.saved': 'Saved',
  'settings.memory.embed.saved.restart': 'Saved. Restart the gateway to switch.',
  'settings.memory.embed.unchanged': 'Nothing changed.',
  'settings.memory.embed.afterSave':
    'Saved for the next start. After the restart, stored passages are re-read for the new engine; rebuild the index if search looks off.',
  'settings.memory.embed.ftsOnly':
    'Search by meaning is turned off for every engine in the configuration.',
  'settings.memory.embed.ftsOnly.fix': 'Turn it back on',
  'settings.memory.embed.ftsOnly.done': 'Turned on. Restart the gateway to apply.',
  'settings.memory.ollama.checking': 'Looking for Ollama at {url}…',
  'settings.memory.ollama.down':
    'Ollama is not running at {url}. Start the Ollama app before you restart the gateway, or search by meaning will not work.',
  'settings.memory.ollama.noModel':
    'Ollama is running, but {model} is not downloaded. Run “ollama pull {model}” first.',
  'settings.memory.ollama.ready': 'Ollama is running and has {model}.',
  'settings.memory.ollama.up': 'Ollama is running.',
  'settings.memory.ollama.remote':
    'Ollama has to be reachable from the gateway’s machine at {url}; the app cannot check that from here.',
  'settings.memory.ollama.again': 'Check again',

  // Setup: recall
  'settings.memory.recall.card.title': 'Recall before each reply',
  'settings.memory.recall.card.blurb':
    'Before answering, the agent looks through its memory for anything related to your message.',
  'settings.memory.recall.enabled': 'Look things up before replying',
  'settings.memory.recall.enabled.help':
    'Only in your own chats, never in scheduled or background runs.',
  'settings.memory.recall.mode': 'How hard to look',
  'settings.memory.recall.mode.escalate': 'Dig deeper for the past',
  'settings.memory.recall.mode.escalate.blurb':
    'A quick lookup every time, a deeper one when you ask what was said or decided before.',
  'settings.memory.recall.mode.always': 'Always dig deeper',
  'settings.memory.recall.mode.always.blurb':
    'A deeper lookup before every reply. Slower, and one small model call each time.',
  'settings.memory.recall.mode.off': 'Quick lookup only',
  'settings.memory.recall.mode.off.blurb': 'Never the deeper lookup. Fastest.',
  'settings.memory.recall.advanced.hint': '{chars} characters · {seconds} s · {model}',
  'settings.memory.recall.maxChars': 'Most text added per reply',
  'settings.memory.recall.maxChars.help': 'From 200 to 8,000 characters.',
  'settings.memory.recall.timeout': 'Time limit for the deeper lookup',
  'settings.memory.recall.timeout.help': 'Up to 120 seconds. The reply never waits longer.',
  'settings.memory.recall.model': 'Model for the deeper lookup',
  'settings.memory.recall.model.help':
    'Empty uses the model you are chatting with. Pick a fast one if replies feel slow.',
  'settings.memory.recall.model.inherit': 'Chat model',
  'settings.memory.recall.provider': 'Provider of that model',
  'settings.memory.recall.provider.help': 'Only needed when the model comes from another provider.',
  'settings.memory.recall.invalid.chars':
    'Most text added per reply: a whole number from 200 to 8,000.',
  'settings.memory.recall.invalid.timeout': 'Time limit: more than 0 and at most 120 seconds.',

  // Setup: learning
  'settings.memory.review.title': 'Learn from conversations',
  'settings.memory.review.blurb':
    'Every few messages the agent re-reads the chat in the background and saves what is worth keeping.',
  'settings.memory.review.enabled': 'Review conversations for facts',
  'settings.memory.review.enabled.help': 'Runs after the reply, so it never slows you down.',
  'settings.memory.review.interval': 'Review every',
  'settings.memory.review.interval.help': 'Counted in messages you send.',
  'settings.memory.review.custom': 'Custom',
  'settings.memory.review.compaction': 'Review before a long chat is condensed',
  'settings.memory.review.compaction.help':
    'One last look before older messages are summarised, so nothing durable is lost.',
  'settings.memory.review.advanced.hint': '{seconds} s per review',
  'settings.memory.review.timeout': 'Time limit for a review',
  'settings.memory.review.timeout.help': 'A review that runs longer is dropped.',
  'settings.memory.review.invalid.interval': 'Review every 1 message or more.',
  'settings.memory.review.invalid.timeout': 'Give a review a time limit above 0 seconds.',

  // Setup: limits
  'settings.memory.limits.title': 'Limits',
  'settings.memory.limits.blurb':
    'How much the agent keeps in mind at all times. Bigger lists cost more on every message.',
  'settings.memory.limits.used': '{used} used now · default {default}.',
  'settings.memory.limits.work.help': 'Default 4,000.',
  'settings.memory.limits.you.help': 'Default 2,000.',
  'settings.memory.limits.inject': 'Added to each message',
  'settings.memory.limits.inject.help': 'Both lists together, with their headings. Default 6,400.',
  'settings.memory.limits.tooSmall':
    'Too small for both lists: once they fill up, one would be left out of every message. Raise it to at least {needed}.',
  'settings.memory.limits.use': 'Use {needed}',
  'settings.memory.limits.belowUse':
    '{list} already holds {used}, more than a limit of {limit}. Remove or shorten facts under Remembered first, or keep the limit at {min} or more.',
  'settings.memory.limits.belowUse.short': 'A limit is below what its list holds now.',
  'settings.memory.limits.invalid': 'Use whole numbers of 100 or more.',

  // Setup: maintenance
  'settings.memory.maint.title': 'Maintenance',
  'settings.memory.maint.blurb': 'Rarely needed. Memory keeps itself up to date as you chat.',
  'settings.memory.maint.reindex': 'Pick up changed files',
  'settings.memory.maint.reindex.help':
    'Reads notes and documents again after you edit them outside the app.',
  'settings.memory.maint.reindex.button': 'Reindex',
  'settings.memory.maint.reindexing': 'Reindexing…',
  'settings.memory.maint.reindexed': 'Index up to date: {chunks} from {files}.',
  'settings.memory.maint.rebuild': 'Rebuild the index',
  'settings.memory.maint.rebuild.help':
    'Starts over from the files. Use it after changing how memory is searched.',
  'settings.memory.maint.rebuild.button': 'Rebuild index',
  'settings.memory.maint.rebuilding': 'Rebuilding…',
  'settings.memory.maint.rebuild.confirm.title': 'Rebuild the index?',
  'settings.memory.maint.rebuild.confirm.body':
    'Every note and document is read and indexed again. Searches may miss things until it finishes.',
  'settings.memory.maint.rebuilt': 'Index rebuilt: {chunks} from {files}.',
  'settings.memory.maint.forget': 'Forget a conversation',
  'settings.memory.maint.forget.help':
    'Removes what the agent learned from one chat. You see what goes before anything is removed.',
  'settings.memory.maint.forget.button': 'Choose…',

  // Forget sheet
  'settings.memory.forget.title': 'Forget a conversation',
  'settings.memory.forget.blurb':
    'Pick a chat to see what it taught the agent. Nothing is removed until you confirm.',
  'settings.memory.forget.session': 'Conversation',
  'settings.memory.forget.pick': 'Choose a conversation',
  'settings.memory.forget.noSessions': 'No conversations yet',
  'settings.memory.forget.manual': 'Another one, by session id or key…',
  'settings.memory.forget.manual.label': 'Session id or key',
  'settings.memory.forget.manual.placeholder': 'agent:main:…',
  'settings.memory.forget.manual.help':
    'The full id or key, as shown in the session’s details. A deleted chat can still be forgotten this way.',
  'settings.memory.forget.preview': 'Show what would go',
  'settings.memory.forget.checking': 'Checking…',
  'settings.memory.forget.back': 'Back',
  'settings.memory.forget.confirm': 'Forget',
  'settings.memory.forget.forgetting': 'Forgetting…',
  'settings.memory.forget.summary': 'Forgetting this chat removes {facts}, {notes} and {passages}.',
  'settings.memory.forget.summary.done': 'Removed {facts}, {notes} and {passages}.',
  'settings.memory.forget.nothing': 'Nothing the agent remembers came from this conversation.',
  'settings.memory.forget.nothingLeft': 'Done. Nothing from this conversation is left in memory.',
  'settings.memory.forget.already': 'This conversation was forgotten before.',
  'settings.memory.forget.facts': 'Facts',
  'settings.memory.forget.notes': 'Note passages',
  'settings.memory.forget.mixed': 'Also learned in another chat',
  'settings.memory.forget.notFound':
    'No conversation of this agent has that id or key. Nothing was changed.',
  'settings.memory.forget.done': 'Forgot {facts} and {notes}.',
} as const
