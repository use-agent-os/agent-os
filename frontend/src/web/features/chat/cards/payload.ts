import { useQuery } from '@tanstack/react-query'
import { useRpc } from '@/app/providers'
import { artifactDownloadUrl, type Artifact } from '@/views/chat/transcript/artifacts'
import { fetchArtifact, type TicketRpc } from '../artifact-files'
import { useChatActions } from '../transcript/context'

/** Fetch an artifact's JSON body (card payloads are not inline) through a ticket. */
export async function fetchArtifactJson(
  rpc: TicketRpc,
  artifact: Artifact,
  sessionKey: string,
): Promise<unknown> {
  return (await fetchArtifact(rpc, artifact, sessionKey)).json()
}

export function useArtifactJson(artifact: Artifact) {
  const rpc = useRpc()
  const { sessionKey } = useChatActions()
  return useQuery({
    queryKey: ['artifact', sessionKey, artifact.id ?? artifactDownloadUrl(artifact)],
    queryFn: () => fetchArtifactJson(rpc, artifact, sessionKey),
    staleTime: Infinity,
  })
}
