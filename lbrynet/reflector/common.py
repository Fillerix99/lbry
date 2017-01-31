REFLECTOR_V1 = 0
REFLECTOR_V2 = 1


class ReflectorClientVersionError(Exception):
    pass


class ReflectorRequestError(Exception):
    pass


class ReflectorRequestDecodeError(Exception):
    pass


class IncompleteResponse(Exception):
    pass
