angular.module('app').directive("validateBitcoinAddress", function() {
    return {
        require: 'ngModel',
        link: function(scope, ele, attrs, ctrl) {

            ctrl.$parsers.unshift(function(value) {
                var valid = window.bitcoinAddress.validate(value);
                ctrl.$setValidity('validateBitcoinAddress', valid);
                return valid ? value : undefined;
            });

            ctrl.$formatters.unshift(function(value) {
                var valid = window.bitcoinAddress.validate(value);
                ctrl.$setValidity('validateBitcoinAddress', valid);
                return value;
            });


        }
    };

});
