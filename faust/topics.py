import asyncio
import re
import typing
from collections import defaultdict
from typing import (
    Any, Awaitable, Callable, Iterable, Iterator, Mapping,
    MutableMapping, Optional, Pattern, Sequence, Set, Union, cast,
)
from .channels import Channel
from .types import (
    AppT, CodecArg, FutureMessage, K, Message,
    ModelArg, PendingMessage, RecordMetadata, TopicPartition, V,
)
from .types.topics import ChannelT, TopicManagerT, TopicT
from .types.transports import ConsumerCallback, TPorTopicSet
from .utils.futures import notify
from .utils.logging import get_logger
from .utils.services import Service
from .utils.times import Seconds

if typing.TYPE_CHECKING:
    from .app import App
else:
    class App: ...  # noqa

__all__ = [
    'Topic',
    'TopicManager',
]

__flake8_Awaitable_is_used: Awaitable            # XXX flake8 bug
__flake8_Callable_is_used: Callable              # XXX flake8 bug
__flake8_PendingMessage_is_used: PendingMessage  # XXX flake8 bug
__flake8_RecordMetadata_is_used: RecordMetadata  # XXX flake8 bug

logger = get_logger(__name__)


class Topic(Channel, TopicT):
    """Define new topic description.

    Arguments:
        app (AppT): App instance this topic is bound to.

    Keyword Arguments:
        topics: List of topic names.
        partitions: Number of partitions for these topics.
            On declaration, topics are created using this.
            Note: kafka cluster configuration is used if message produced
            when topic not declared.
        retention: Number of seconds (float/timedelta) to keep messages
            in the topic before they expire.
        pattern: Regular expression to match.
            You cannot specify both topics and a pattern.
        key_type: Model used for keys in this topic.
        value_type: Model used for values in this topic.

    Raises:
        TypeError: if both `topics` and `pattern` is provided.
    """

    _declared = False
    _partitions: int = None
    _replicas: int = None
    _pattern: Pattern = None

    def __init__(self, app: AppT,
                 *,
                 topics: Sequence[str] = None,
                 pattern: Union[str, Pattern] = None,
                 key_type: ModelArg = None,
                 value_type: ModelArg = None,
                 is_iterator: bool = False,
                 partitions: int = None,
                 retention: Seconds = None,
                 compacting: bool = None,
                 deleting: bool = None,
                 replicas: int = None,
                 acks: bool = True,
                 config: Mapping[str, Any] = None,
                 loop: asyncio.AbstractEventLoop = None) -> None:
        self.topics = topics
        super().__init__(
            app,
            key_type=key_type,
            value_type=value_type,
            loop=loop,
            is_iterator=is_iterator,
        )
        self.pattern = cast(Pattern, pattern)  # XXX mypy does not read setter
        self.partitions = partitions
        self.retention = retention
        self.compacting = compacting
        self.deleting = deleting
        self.replicas = replicas
        self.config = config or {}

    def _clone_args(self) -> Mapping:
        return {**super()._clone_args(), **{
            'topics': self.topics,
            'pattern': self.pattern,
            'partitions': self.partitions,
            'retention': self.retention,
            'compacting': self.compacting,
            'deleting': self.deleting,
            'replicas': self.replicas,
            'config': self.config,
        }}

    @property
    def pattern(self) -> Optional[Pattern]:
        return self._pattern

    @pattern.setter
    def pattern(self, pattern: Union[str, Pattern]) -> None:
        if pattern and self.topics:
            raise TypeError('Cannot specify both topics and pattern')
        if isinstance(pattern, str):
            pattern = re.compile(pattern)
        self._pattern = pattern

    @property
    def partitions(self) -> int:
        return self._partitions

    @partitions.setter
    def partitions(self, partitions: int) -> None:
        if partitions is None:
            partitions = self.app.default_partitions
        if partitions == 0:
            raise ValueError('Topic cannot have 0 (zero partitions)')
        self._partitions = partitions

    @property
    def replicas(self) -> int:
        return self._replicas

    @replicas.setter
    def replicas(self, replicas: int) -> None:
        if replicas is None:
            replicas = self.app.replication_factor
        self._replicas = replicas

    def derive(self,
               *,
               topics: Sequence[str] = None,
               key_type: ModelArg = None,
               value_type: ModelArg = None,
               partitions: int = None,
               retention: Seconds = None,
               compacting: bool = None,
               deleting: bool = None,
               config: Mapping[str, Any] = None,
               prefix: str = '',
               suffix: str = '') -> TopicT:
        """Create new :class:`Topic` derived from this topic.

        Configuration will be copied from this topic, but any parameter
        overriden as a keyword argument.
        """
        topics = self.topics if topics is None else topics
        if suffix or prefix:
            if self.pattern:
                raise ValueError(
                    'Cannot add prefix/suffix to Topic with pattern')
                topics = [f'{prefix}{topic}{suffix}' for topic in topics]
        return type(self)(
            self.app,
            topics=topics,
            pattern=self.pattern,
            key_type=self.key_type if key_type is None else key_type,
            value_type=self.value_type if value_type is None else value_type,
            partitions=self.partitions if partitions is None else partitions,
            retention=self.retention if retention is None else retention,
            compacting=self.compacting if compacting is None else compacting,
            deleting=self.deleting if deleting is None else deleting,
            config=self.config if config is None else config,
        )

    def get_topic_name(self) -> str:
        return self.topics[0]

    async def publish_message(self, fut: FutureMessage,
                              wait: bool = True) -> FutureMessage:
        app = self.app
        message: PendingMessage = fut.message
        if isinstance(message.channel, str):
            topic = message.channel
        elif isinstance(message.channel, TopicT):
            topic = cast(TopicT, message.channel).get_topic_name()
        else:
            topic = self.get_topic_name()
        key: bytes = cast(bytes, message.key)
        value: bytes = cast(bytes, message.value)
        logger.debug('send: topic=%r key=%r value=%r', topic, key, value)
        assert topic is not None
        producer = await app.maybe_start_producer()
        state = await app.sensors.on_send_initiated(
            producer, topic,
            keysize=len(key) if key else 0,
            valsize=len(value) if value else 0)
        if wait:
            ret: RecordMetadata = await producer.send_and_wait(
                topic, key, value, partition=message.partition)
            await app.sensors.on_send_completed(producer, state)
            return await self._finalize_message(fut, ret)
        else:
            await producer.send(topic, key, value, partition=message.partition)
            # XXX add done callback
            # XXX call sensors
            return fut

    def prepare_key(self,
                    key: K,
                    key_serializer: CodecArg) -> Any:
        if key is not None:
            return self.app.serializers.dumps_key(key, key_serializer)
        return None

    def prepare_value(self,
                      value: V,
                      value_serializer: CodecArg) -> Any:
        return self.app.serializers.dumps_value(value, value_serializer)

    async def maybe_declare(self) -> None:
        if not self._declared:
            self._declared = True
            await self.declare()

    async def declare(self) -> None:
        producer = await self.app.maybe_start_producer()
        for topic in self.topics:
            await producer.create_topic(
                topic=topic,
                partitions=self.partitions,
                replication=self.replicas,
                config=self.config,
            )

    def __aiter__(self) -> ChannelT:
        channel = self.clone(is_iterator=True)
        self.app.channels.add(channel)
        return channel

    def __str__(self) -> str:
        return str(self.pattern) if self.pattern else ','.join(self.topics)


class TopicManager(TopicManagerT, Service):
    """Manages the channels that subscribe to topics.

    - Consumes messages from topic using a single consumer.
    - Forwards messages to all channels subscribing to a topic.
    """
    logger = logger

    #: Fast index to see if Topic is registered.
    _topics: Set[TopicT]

    #: Map str topic to set of channeos that should get a copy
    #: of each message sent to that topic.
    _topicmap: MutableMapping[str, Set[TopicT]]

    _pending_tasks: asyncio.Queue

    #: Whenever a change is made, i.e. a Topic is added/removed, we notify
    #: the background task responsible for resubscribing.
    _subscription_changed: Optional[asyncio.Event]

    _subscription_done: Optional[asyncio.Future]

    def __init__(self, app: AppT, **kwargs: Any) -> None:
        Service.__init__(self, **kwargs)
        self.app = app
        self._topics = set()
        self._topicmap = defaultdict(set)
        self._pending_tasks = asyncio.Queue(loop=self.loop)

        self._subscription_changed = None
        self._subscription_done = None
        # we compile the closure used for receive messages
        # (this just optimizes symbol lookups, localizing variables etc).
        self.on_message: Callable[[Message], Awaitable[None]]
        self.on_message = self._compile_message_handler()

    async def commit(self, topics: TPorTopicSet) -> bool:
        return await self.app.consumer.commit(topics)

    def _compile_message_handler(self) -> ConsumerCallback:
        wait = asyncio.wait
        all_completed = asyncio.ALL_COMPLETED
        loop = self.loop
        list_ = list
        # topic str -> list of TopicT
        get_channels_for_topic = self._topicmap.__getitem__

        add_pending_task = self._pending_tasks.put

        async def on_message(message: Message) -> None:
            # when a message is received we find all channels
            # that subscribe to this message
            channels = list_(get_channels_for_topic(message.topic))

            # we increment the reference count for this message in bulk
            # immediately, so that nothing will get a chance to decref to
            # zero before we've had the chance to pass it to all channels
            message.incref_bulk(channels)

            # Then send it to each channels buffer
            # for Channel.__anext__ to pick up.
            # NOTE: We do this in parallel, so the order of channels
            #       does not matter.
            await wait(
                [add_pending_task(channel.deliver(message))
                 for channel in channels],
                loop=loop,
                return_when=all_completed,
            )
        return on_message

    @Service.task
    async def _subscriber(self) -> None:
        # the first time we start, we will wait two seconds
        # to give actors a chance to start up and register their
        # streams.  This way we won't have N subscription requests at the
        # start.
        await self.sleep(2.0)

        # tell the consumer to subscribe to the topics.
        await self.app.consumer.subscribe(self._update_topicmap())
        notify(self._subscription_done)

        # Now we wait for changes
        ev = self._subscription_changed = asyncio.Event(loop=self.loop)
        while not self.should_stop:
            await ev.wait()
            await self.app.consumer.subscribe(self._update_topicmap())
            ev.clear()
            notify(self._subscription_done)

    async def wait_for_subscriptions(self) -> None:
        if self._subscription_done is not None:
            await self._subscription_done

    @Service.task
    async def _gatherer(self) -> None:
        waiting = set()
        wait = asyncio.wait
        first_completed = asyncio.FIRST_COMPLETED
        while not self.should_stop:
            waiting.add(await self._pending_tasks.get())
            finished, unfinished = await wait(
                waiting, return_when=first_completed)
            waiting = unfinished

    def _update_topicmap(self) -> Iterable[str]:
        self._topicmap.clear()
        for channel in self._topics:
            for topic in channel.topics:
                self._topicmap[topic].add(channel)
        return self._topicmap

    async def on_partitions_assigned(
            self, assigned: Iterable[TopicPartition]) -> None:
        ...

    async def on_partitions_revoked(
            self, revoked: Iterable[TopicPartition]) -> None:
        ...

    def __contains__(self, value: Any) -> bool:
        return value in self._topics

    def __iter__(self) -> Iterator[TopicT]:
        return iter(self._topics)

    def __len__(self) -> int:
        return len(self._topics)

    def __hash__(self) -> int:
        return object.__hash__(self)

    def add(self, topic: Any) -> None:
        if topic not in self._topics:
            self._topics.add(topic)
            self.beacon.add(topic)  # connect to beacon
            self._flag_changes()

    def discard(self, topic: Any) -> None:
        self._topics.discard(topic)
        self.beacon.discard(topic)
        self._flag_changes()

    def _flag_changes(self) -> None:
        if self._subscription_changed is not None:
            self._subscription_changed.set()
        if self._subscription_done is None:
            self._subscription_done = asyncio.Future(loop=self.loop)

    @property
    def label(self) -> str:
        return f'{type(self).__name__}({len(self._topics)})'

    @property
    def shortlabel(self) -> str:
        return type(self).__name__
