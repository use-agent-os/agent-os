import { describe, expect, it } from 'vitest'
import { legacyRedirect } from './legacy-redirect'

describe('legacyRedirect', () => {
  it.each([
    ['/overview', '', '/home'],
    ['/cron', '', '/automations'],
    ['/cron', '?job=j1', '/automations?job=j1'],
    ['/projects', '?project=P', '/projects/P'],
    ['/projects', '?project=a%2Fb', '/projects/a%2Fb'],
    ['/chat', '?session=agent:main:webchat:abc', '/chat/main/webchat/abc'],
    ['/chat', '?agent=A', '/chat/~/agent%3AA%3Awebchat%3Adefault'],
    ['/chat', '?agent=research', '/chat/research/webchat/default'],
    ['/chat', '?session=cron:x&agent=main', '/chat/~/cron%3Ax'],
    ['/health', '', '/settings/health'],
    ['/channels', '', '/settings/channels'],
    ['/mcp', '', '/settings/mcp'],
    ['/skills', '', '/settings/skills'],
    ['/memory', '', '/settings/memory'],
    ['/agents', '', '/settings/agents'],
    ['/env', '', '/settings/environment'],
    ['/logs', '', '/settings/logs'],
    ['/approvals', '', '/settings/approvals'],
    ['/config', '', '/settings/advanced'],
    ['/setup', '', '/settings/setup'],
    ['/setup', '?step=router', '/settings/setup?step=router'],
    ['/channels', '?view=setup', '/settings/channels?setup=1'],
    ['/settings', '?step=channels', '/settings/channels?setup=1'],
    ['/settings', '?step=provider', '/settings/setup?step=provider'],
  ])('%s%s -> %s', (path, search, target) => {
    expect(legacyRedirect(path, search)).toBe(target)
  })

  it('maps an agent that is not a normalized id to the reserved chat path', () => {
    // `?agent=A` is not a valid agent id; the redirect still lands on a chat
    // URL that decodes to A's default session.
    expect(legacyRedirect('/chat', '?agent=A')).toMatch(/^\/chat\/~\//)
  })

  it.each([
    ['/chat', ''],
    ['/chat/main/webchat/default', ''],
    ['/home', ''],
    ['/sessions', '?q=x'],
    ['/projects', ''],
    ['/projects/P', ''],
    ['/usage', ''],
    ['/inbox', ''],
    ['/settings', ''],
    ['/settings/channels', ''],
    ['/mcp/oauth/callback', '?code=c&state=s'],
    ['/new', ''],
  ])('leaves current route %s%s alone', (path, search) => {
    expect(legacyRedirect(path, search)).toBeNull()
  })
})
