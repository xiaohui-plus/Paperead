import { defineTool } from '@deepseek-ai/dsh-tools'

export const name = 'research-tools'
export const inject = ['tools']

export function apply(ctx) {
  ctx.tools.register(
    defineTool({
      name: 'echo',
      description:
        'Echo the given text back verbatim. Used by the Paperead research host spike to prove the tool pipeline and card projection.',
      parameters: {
        text: {
          type: 'string',
          required: true,
          description: 'Text to echo back.',
        },
      },
      output: {
        schema: { type: 'string' },
        render: (_args, value) => [{ type: 'text', text: value }],
      },
      async execute(args) {
        return args.text
      },
      presentCall(args) {
        return {
          card: 'generic',
          kind: 'echo',
          title: '回声',
          rawInput: args,
        }
      },
      presentResult(_args, value) {
        return {
          card: 'generic',
          title: '回声完成',
          content: value,
        }
      },
    }),
  )
}
