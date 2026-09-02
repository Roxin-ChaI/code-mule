"""Line-oriented interactive Boss chat loop."""

from typing import TextIO

from .service import BossConversationService


def run_chat_loop(
    service: BossConversationService,
    *,
    input_stream: TextIO,
    output_stream: TextIO,
) -> None:
    print("Code Mule chat. Type 'help' for capabilities. Ctrl+C or EOF exits.", file=output_stream)
    while True:
        try:
            output_stream.write("You > ")
            output_stream.flush()
            raw = input_stream.readline()
        except KeyboardInterrupt:
            print("\nCode Mule > Goodbye.", file=output_stream)
            return
        if raw == "":
            print("\nCode Mule > Goodbye.", file=output_stream)
            return
        message = raw.strip()
        if message == "":
            continue
        reply = service.handle(message)
        first, *rest = reply.lines
        print(f"Code Mule > {first}", file=output_stream)
        for line in rest:
            print(f"            {line}" if line else "", file=output_stream)


__all__ = ["run_chat_loop"]
