class DaybaggerError(Exception): pass
class ConfigurationError(DaybaggerError): pass
class GoldenRulesError(DaybaggerError): pass
class InvalidMarketDataError(DaybaggerError): pass
class ExecutionBlockedError(DaybaggerError): pass
